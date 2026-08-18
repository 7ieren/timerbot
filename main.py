import discord
from discord.ext import commands, tasks
from discord import app_commands
import asyncio
import json
import math
import os
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────
DATA_FILE = "data.json"

# Warning rule: bosses with a short respawn get a 1-minute warning ping,
# anything longer gets a 5-minute warning. Tweak the cutoff to taste.
SHORT_RESPAWN_CUTOFF_MINUTES = 60
SHORT_WARN_MINUTES = 1
LONG_WARN_MINUTES = 5

# Who gets pinged by a warning when the boss has no role configured.
DEFAULT_PING = "@everyone"

# The board shows a computed "5h 53m" countdown, which is a static string once
# posted — so it has to be re-edited on an interval to stay honest. Ticks that
# would render an identical board are skipped, so a short interval here buys
# responsiveness at the minute rollover without extra API calls.
BOARD_REFRESH_SECONDS = 10

# Optional starter presets so you don't have to /addboss everything by hand.
# Feel free to edit/delete these — they're just a convenience seed.
# Values are minutes, and may be fractional: write `29 + 52 / 60` for 29m52s.
BOSS_PRESETS = {
    "platanista": 168 * 60,
    "caligo": 168 * 60,
    "darlene": 72 * 60,
    "aiyo": 72 * 60,
    "sephia": 72 * 60,
    "illust": 72 * 60,
    "bigmama": 48 * 60,
    "barslaf": 48 * 60,
    "ukpana": 48 * 60,
    "soul-lich": 24 * 60 + 5,
    "faith":    5 * 60 + 53,
    "billiard": 7 * 60 + 55,
    "actaemon": 6 * 60,
    "devilang": 5 * 60 + 33,
    "wadangka": 2 * 60 + 30,
    "awakenkooii": 1 * 60 + 3,
    "glucose":  30,
    "overload": 29 + 52 / 60,
    "apapa":    15,
}

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)


# ── Persistence ───────────────────────────────────────────────────────────────
# Schema:
# {
#   "<guild_id>": {
#     "board_channel_id": 123,
#     "board_message_id": 456,
#     "bosses": {
#       "faith": {"respawn_minutes": 353, "warn_minutes": 5, "role_id": null}
#     },
#     "timers": {
#       "faith": {"respawns_at": "2026-08-16T05:53:00+00:00", "reported_by": "someone"}
#     }
#   }
# }

def load_data() -> dict:
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    return {}


def save_data(data: dict) -> None:
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)


store: dict = load_data()

# In-memory only: guild_id -> {boss_name: asyncio.Task}
running_tasks: dict = {}

# In-memory only: guild_id -> last board text actually pushed to Discord,
# used to skip redundant edits.
board_cache: dict = {}


def guild_state(guild_id: int) -> dict:
    key = str(guild_id)
    if key not in store:
        store[key] = {"board_channel_id": None, "board_message_id": None, "bosses": {}, "timers": {}}
    store[key].setdefault("bosses", {})
    store[key].setdefault("timers", {})
    return store[key]


def default_warn_minutes(respawn_minutes: int) -> int:
    return SHORT_WARN_MINUTES if respawn_minutes <= SHORT_RESPAWN_CUTOFF_MINUTES else LONG_WARN_MINUTES


def format_duration(total_minutes: float) -> str:
    total_seconds = max(0, round(total_minutes * 60))
    d, remainder = divmod(total_seconds, 86400)
    h, remainder = divmod(remainder, 3600)
    m, s = divmod(remainder, 60)
    parts = []
    if d: parts.append(f"{d}d")
    if h: parts.append(f"{h}h")
    if m: parts.append(f"{m}m")
    if s: parts.append(f"{s}s")
    return " ".join(parts) if parts else "0m"


def format_remaining(respawns_at: datetime, now: datetime) -> str:
    remaining_seconds = (respawns_at - now).total_seconds()
    if remaining_seconds <= 0:
        return "due"
    # Round up so a timer never reads "0m" while it's still counting down.
    return format_duration(math.ceil(remaining_seconds / 60))


# ── Board message (single updating timer list) ───────────────────────────────

def build_board_embed(guild_id: int) -> discord.Embed:
    state = guild_state(guild_id)
    timers = state["timers"]

    if not timers:
        return discord.Embed(
            title="JR's Boss Timers",
            description="No active timers :(",
            color=discord.Color.dark_grey(),
        )

    now = datetime.now(timezone.utc)
    rows = []
    for boss_name, info in sorted(timers.items(), key=lambda kv: kv[1]["respawns_at"]):
        respawns_at = datetime.fromisoformat(info["respawns_at"])
        unix_ts = int(respawns_at.timestamp())
        rows.append(
            f"**{boss_name.title()}** — **{format_remaining(respawns_at, now)}** "
            f"(<t:{unix_ts}:t>)"
        )

    embed = discord.Embed(
        title="JR's Boss Timers",
        description="\n".join(rows),
        color=discord.Color.blurple(),
    )
    embed.timestamp = now
    embed.set_footer(text="Updated")
    return embed


async def get_board_channel(guild: discord.Guild) -> discord.TextChannel | None:
    state = guild_state(guild.id)
    if not state["board_channel_id"]:
        return None
    channel = guild.get_channel(state["board_channel_id"])
    return channel


async def refresh_board(guild: discord.Guild, repost: bool = False) -> None:
    """Update the board in place, or with repost=True move it to the channel bottom."""
    state = guild_state(guild.id)
    channel = await get_board_channel(guild)
    if not channel:
        return

    embed = build_board_embed(guild.id)
    rendered = embed.description or ""

    # The countdown is minute-granular, so most ticks render identically to the
    # last one. Skip those instead of spending an API call to change nothing.
    if not repost and state["board_message_id"] and board_cache.get(guild.id) == rendered:
        return

    if repost and state["board_message_id"]:
        # Delete the old board first — two boards in one channel means the
        # stale one keeps showing countdowns nothing is refreshing.
        try:
            old = await channel.fetch_message(state["board_message_id"])
            await old.delete()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass
        state["board_message_id"] = None

    msg = None
    if state["board_message_id"]:
        try:
            msg = await channel.fetch_message(state["board_message_id"])
        except (discord.NotFound, discord.HTTPException):
            msg = None

    if msg is None:
        msg = await channel.send(embed=embed)
        state["board_message_id"] = msg.id
        save_data(store)
    else:
        try:
            await msg.edit(embed=embed)
        except discord.HTTPException:
            # Forget the cached render so the next tick retries rather than
            # assuming the board is up to date.
            board_cache.pop(guild.id, None)
            return

    board_cache[guild.id] = rendered


# ── Timer logic ───────────────────────────────────────────────────────────────

def resolve_died_at(now: datetime, died_at_minute: int | None, died_at_dt: datetime | None) -> datetime:
    """The moment the boss actually died, from whichever form the reporter gave."""
    if died_at_dt is not None:
        return died_at_dt
    if died_at_minute is not None:
        # ":47" means the most recent :47 at or before now.
        elapsed_mins = (now.minute - died_at_minute) % 60
        return now - timedelta(minutes=elapsed_mins, seconds=now.second, microseconds=now.microsecond)
    return now


async def start_timer(guild: discord.Guild, boss_name: str, respawns_at: datetime, reported_by: str, repost: bool = False) -> None:
    state = guild_state(guild.id)

    # Cancel any existing run for this boss
    task_map = running_tasks.setdefault(guild.id, {})
    if boss_name in task_map:
        task_map[boss_name].cancel()

    state["timers"][boss_name] = {
        "respawns_at": respawns_at.isoformat(),
        "reported_by": reported_by,
    }
    save_data(store)
    await refresh_board(guild, repost=repost)

    task_map[boss_name] = asyncio.create_task(_run_timer(guild, boss_name))


async def _run_timer(guild: discord.Guild, boss_name: str) -> None:
    state = guild_state(guild.id)
    channel = await get_board_channel(guild)

    try:
        timer_info = state["timers"][boss_name]
        boss_cfg = state["bosses"][boss_name]
        respawns_at = datetime.fromisoformat(timer_info["respawns_at"])
        warn_at = respawns_at - timedelta(minutes=boss_cfg["warn_minutes"])
        now = datetime.now(timezone.utc)

        unix_ts = int(respawns_at.timestamp())
        role = guild.get_role(boss_cfg["role_id"]) if boss_cfg.get("role_id") else None
        ping = role.mention if role else DEFAULT_PING

        # The warning ping is kept around so the respawn can be announced by
        # editing it in place, rather than posting a second message.
        warn_msg = None

        warn_sleep = (warn_at - now).total_seconds()
        if warn_sleep > 0:
            await asyncio.sleep(warn_sleep)
            if channel:
                warn_msg = await channel.send(
                    f"{ping} **{boss_name.title()}** respawns in "
                    f"**{boss_cfg['warn_minutes']}m** — <t:{unix_ts}:t>"
                )

        now = datetime.now(timezone.utc)
        final_sleep = (respawns_at - now).total_seconds()
        if final_sleep > 0:
            await asyncio.sleep(final_sleep)

        respawned_text = (
            f"{ping} **{boss_name.title()}** has respawned at <t:{unix_ts}:t> "
            f"(<t:{unix_ts}:R>)"
        )
        if warn_msg is not None:
            try:
                await warn_msg.edit(content=respawned_text)
            except discord.HTTPException:
                pass
        elif channel:
            # No warning went out (timer started inside the warning window), so
            # there's nothing to edit — announce the respawn directly.
            await channel.send(respawned_text)

    except asyncio.CancelledError:
        return
    finally:
        state["timers"].pop(boss_name, None)
        save_data(store)
        task_map = running_tasks.get(guild.id, {})
        task_map.pop(boss_name, None)
        await refresh_board(guild)


# ── Boss / timer autocomplete ─────────────────────────────────────────────────

async def boss_autocomplete(interaction: discord.Interaction, current: str):
    bosses = guild_state(interaction.guild_id)["bosses"]
    return [
        app_commands.Choice(name=name, value=name)
        for name in bosses
        if current.lower() in name.lower()
    ][:25]


async def active_timer_autocomplete(interaction: discord.Interaction, current: str):
    timers = guild_state(interaction.guild_id)["timers"]
    return [
        app_commands.Choice(name=name, value=name)
        for name in timers
        if current.lower() in name.lower()
    ][:25]


# ── Commands ───────────────────────────────────────────────────────────────────

@bot.tree.command(name="setboard", description="Set the channel where the timer list and notifications are posted.")
@app_commands.describe(channel="The channel to use as the timer board")
@app_commands.checks.has_permissions(manage_channels=True)
async def setboard(interaction: discord.Interaction, channel: discord.TextChannel):
    state = guild_state(interaction.guild_id)
    state["board_channel_id"] = channel.id
    state["board_message_id"] = None
    save_data(store)
    await interaction.response.send_message(f"✅ Timer board set to {channel.mention}.", ephemeral=True)
    await refresh_board(interaction.guild)


@bot.tree.command(name="addboss", description="Register a boss and its respawn time.")
@app_commands.describe(
    name="Boss name",
    days="Respawn days", hours="Respawn hours", minutes="Respawn minutes", seconds="Respawn seconds",
    role="Role to ping on the warning (optional)",
    warn_minutes="Override the auto warning time (optional — default is 1 or 5 min based on respawn length)",
)
@app_commands.checks.has_permissions(manage_channels=True)
async def addboss(
    interaction: discord.Interaction,
    name: str,
    days: int = 0,
    hours: int = 0,
    minutes: int = 0,
    seconds: int = 0,
    role: discord.Role = None,
    warn_minutes: int = None,
):
    total_minutes = days * 1440 + hours * 60 + minutes + seconds / 60
    if total_minutes <= 0:
        await interaction.response.send_message(
            "Provide at least one of: days, hours, minutes, seconds.", ephemeral=True
        )
        return

    boss_name = name.lower().strip()
    state = guild_state(interaction.guild_id)
    state["bosses"][boss_name] = {
        "respawn_minutes": total_minutes,
        "warn_minutes": warn_minutes if warn_minutes is not None else default_warn_minutes(total_minutes),
        "role_id": role.id if role else None,
    }
    save_data(store)

    await interaction.response.send_message(
        f"✅ **{boss_name.title()}** registered.\n"
        f"• Respawn: **{format_duration(total_minutes)}**\n"
        f"• Warning ping: **{state['bosses'][boss_name]['warn_minutes']}m** before respawn\n"
        f"• Ping role: {role.mention if role else f'*(none — {DEFAULT_PING})*'}",
        ephemeral=True,
    )


@bot.tree.command(name="removeboss", description="Unregister a boss.")
@app_commands.describe(boss="Boss to remove")
@app_commands.autocomplete(boss=boss_autocomplete)
@app_commands.checks.has_permissions(manage_channels=True)
async def removeboss(interaction: discord.Interaction, boss: str):
    state = guild_state(interaction.guild_id)
    boss = boss.lower()
    if boss not in state["bosses"]:
        await interaction.response.send_message(f"**{boss}** is not registered.", ephemeral=True)
        return
    del state["bosses"][boss]
    save_data(store)
    await interaction.response.send_message(f"\U0001f5d1️ **{boss.title()}** removed.", ephemeral=True)


@bot.tree.command(name="bosses", description="List all registered bosses.")
async def bosses_cmd(interaction: discord.Interaction):
    state = guild_state(interaction.guild_id)
    if not state["bosses"]:
        await interaction.response.send_message("No bosses registered yet. Use `/addboss`.", ephemeral=True)
        return
    # Shortest respawn first; name breaks ties so bosses on the same timer
    # keep a stable order between calls.
    ordered = sorted(state["bosses"].items(), key=lambda kv: (kv[1]["respawn_minutes"], kv[0]))
    lines = [
        f"• **{name.title()}** — {format_duration(cfg['respawn_minutes'])} respawn, "
        f"{cfg['warn_minutes']}m warning"
        for name, cfg in ordered
    ]
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


# Slash commands have no alias mechanism — /died and /d are two registered
# commands sharing one implementation and one set of parameter descriptions.
DIED_DESCRIPTIONS = {
    "boss": "Boss that died",
    "minute": "Minute it died at this hour (0-59) — leave empty if it just died",
    "time": "Exact time it died, e.g. 14:30 or 23/05 14:30 (UTC unless utc_offset is set)",
    "utc_offset": "Your UTC offset, e.g. 8 for UTC+8. Default 0.",
}


async def report_death(
    interaction: discord.Interaction,
    boss: str,
    minute: int = None,
    time: str = None,
    utc_offset: float = 0.0,
):
    state = guild_state(interaction.guild_id)
    boss = boss.lower()
    boss_cfg = state["bosses"].get(boss)

    if not boss_cfg:
        await interaction.response.send_message(f"**{boss}** is not registered. Use `/addboss` first.", ephemeral=True)
        return
    if not state["board_channel_id"]:
        await interaction.response.send_message("No timer board set yet. Ask an admin to run `/setboard`.", ephemeral=True)
        return
    if minute is not None and time is not None:
        await interaction.response.send_message("Provide either `minute` or `time`, not both.", ephemeral=True)
        return
    if minute is not None and not (0 <= minute <= 59):
        await interaction.response.send_message("Minute must be between 0 and 59.", ephemeral=True)
        return

    now = datetime.now(timezone.utc)
    died_at_dt = None

    if time is not None:
        try:
            if " " in time:
                date_part, time_part = time.strip().split(" ", 1)
                day, month = (int(x) for x in date_part.split("/"))
                hour, mins = (int(x) for x in time_part.split(":"))
                year = now.year
            else:
                hour, mins = (int(x) for x in time.strip().split(":"))
                day, month, year = now.day, now.month, now.year

            offset = timezone(timedelta(hours=utc_offset))
            died_at_dt = datetime(year, month, day, hour, mins, tzinfo=offset).astimezone(timezone.utc)
            if died_at_dt > now:
                died_at_dt -= timedelta(days=1)
        except (ValueError, TypeError):
            await interaction.response.send_message(
                "Invalid time format. Use `HH:MM` or `DD/MM HH:MM`.", ephemeral=True
            )
            return

    died_at = resolve_died_at(now, minute, died_at_dt)
    died_ts = int(died_at.timestamp())
    died_note = f"at <t:{died_ts}:t> (<t:{died_ts}:R>)"

    respawns_at = died_at + timedelta(minutes=boss_cfg["respawn_minutes"])
    if respawns_at <= now:
        await interaction.response.send_message(
            "That death time is further back than the respawn timer — it would already be up.", ephemeral=True
        )
        return

    await interaction.response.send_message(f"✅ Timer started for **{boss.title()}**.", ephemeral=True)

    channel = await get_board_channel(interaction.guild)
    if channel:
        await channel.send(
            f"\U0001f480 **{boss.title()}** reported dead {died_note} by {interaction.user.mention}."
        )

    # Repost last so the refreshed timer list is the newest message in the channel.
    await start_timer(
        interaction.guild, boss, respawns_at, reported_by=str(interaction.user), repost=True
    )


@bot.tree.command(name="died", description="Report a boss death and start its respawn timer.")
@app_commands.describe(**DIED_DESCRIPTIONS)
@app_commands.autocomplete(boss=boss_autocomplete)
async def died(
    interaction: discord.Interaction,
    boss: str,
    minute: int = None,
    time: str = None,
    utc_offset: float = 0.0,
):
    await report_death(interaction, boss, minute, time, utc_offset)


@bot.tree.command(name="d", description="Report a boss death and start its respawn timer (short for /died).")
@app_commands.describe(**DIED_DESCRIPTIONS)
@app_commands.autocomplete(boss=boss_autocomplete)
async def died_short(
    interaction: discord.Interaction,
    boss: str,
    minute: int = None,
    time: str = None,
    utc_offset: float = 0.0,
):
    await report_death(interaction, boss, minute, time, utc_offset)


@bot.tree.command(name="cancel", description="Cancel an active boss timer.")
@app_commands.describe(boss="Boss timer to cancel")
@app_commands.autocomplete(boss=active_timer_autocomplete)
async def cancel(interaction: discord.Interaction, boss: str):
    boss = boss.lower()
    state = guild_state(interaction.guild_id)
    task_map = running_tasks.get(interaction.guild_id, {})

    if boss not in state["timers"]:
        await interaction.response.send_message(f"No active timer for **{boss}**.", ephemeral=True)
        return

    if boss in task_map:
        task_map[boss].cancel()
    state["timers"].pop(boss, None)
    save_data(store)
    await refresh_board(interaction.guild)

    await interaction.response.send_message(f"\U0001f5d1️ Timer for **{boss.title()}** cancelled.")


@bot.tree.command(name="seedpresets", description="Register the built-in preset bosses (convenience seed).")
@app_commands.checks.has_permissions(manage_channels=True)
async def seedpresets(interaction: discord.Interaction):
    state = guild_state(interaction.guild_id)
    added = []
    for name, respawn_minutes in BOSS_PRESETS.items():
        if name not in state["bosses"]:
            state["bosses"][name] = {
                "respawn_minutes": respawn_minutes,
                "warn_minutes": default_warn_minutes(respawn_minutes),
                "role_id": None,
            }
            added.append(name.title())
    save_data(store)
    await interaction.response.send_message(
        f"✅ Added: {', '.join(added)}" if added else "All presets were already registered.",
        ephemeral=True,
    )


@bot.tree.command(name="help", description="How to use the boss timer bot.")
async def help_cmd(interaction: discord.Interaction):
    embed = discord.Embed(
        title="Boss Timer — Commands",
        color=discord.Color.blurple(),
    )
    embed.add_field(
        name="Everyone",
        value=(
            "`/died` (or `/d`) — report a boss death and start its timer\n"
            "`/cancel` — cancel an active timer\n"
            "`/bosses` — list registered bosses and their respawn times\n"
            "`/help` — this message"
        ),
        inline=False,
    )
    embed.add_field(
        name="Admin (Manage Channels)",
        value=(
            "`/setboard` — pick the channel for the timer list and pings\n"
            "`/addboss` — register a boss\n"
            "`/removeboss` — unregister a boss\n"
            "`/seedpresets` — register the built-in preset bosses"
        ),
        inline=False,
    )
    embed.add_field(
        name="Reporting a death",
        value=(
            "`/d faith` — it just died\n"
            "`/d faith minute:47` — it died at the most recent :47\n"
            "`/d faith time:14:30 utc_offset:8` — exact time, in UTC+8\n"
            "`/d faith time:23/05 14:30` — with a date, if it died earlier"
        ),
        inline=False,
    )
    embed.set_footer(text=f"Timer list refreshes every {BOARD_REFRESH_SECONDS}s")
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ── Bot lifecycle ─────────────────────────────────────────────────────────────

@tasks.loop(seconds=BOARD_REFRESH_SECONDS)
async def board_refresher():
    for guild in bot.guilds:
        state = guild_state(guild.id)
        # Nothing counting down means nothing to re-edit — skip the API call.
        if not state["timers"] or not state["board_channel_id"]:
            continue
        try:
            await refresh_board(guild)
        except discord.HTTPException:
            pass


@board_refresher.before_loop
async def before_board_refresher():
    await bot.wait_until_ready()


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} slash command(s)")
    except Exception as e:
        print(f"Failed to sync commands: {e}")

    now = datetime.now(timezone.utc)
    for guild in bot.guilds:
        state = guild_state(guild.id)
        for boss_name, info in list(state["timers"].items()):
            respawns_at = datetime.fromisoformat(info["respawns_at"])
            if boss_name not in state["bosses"]:
                state["timers"].pop(boss_name, None)
                continue
            if respawns_at <= now:
                state["timers"].pop(boss_name, None)
                channel = await get_board_channel(guild)
                if channel:
                    unix_ts = int(respawns_at.timestamp())
                    await channel.send(
                        f"**{boss_name.title()}** respawned at <t:{unix_ts}:t> "
                        f"(<t:{unix_ts}:R>) while the bot was offline."
                    )
            else:
                task_map = running_tasks.setdefault(guild.id, {})
                task_map[boss_name] = asyncio.create_task(_run_timer(guild, boss_name))
        save_data(store)
        await refresh_board(guild)

    # on_ready fires again on every gateway re-IDENTIFY, so guard the start.
    if not board_refresher.is_running():
        board_refresher.start()


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("❌ You don't have permission to use this command.", ephemeral=True)
    else:
        if not interaction.response.is_done():
            await interaction.response.send_message(f"❌ An error occurred: `{error}`", ephemeral=True)
        raise error


if __name__ == "__main__":
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise ValueError("No DISCORD_TOKEN found. Set it in .env (see .env.example).")
    bot.run(token)
