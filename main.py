import discord
from discord.ext import commands, tasks
from discord import app_commands
import asyncio
import json
import math
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Literal
from dotenv import load_dotenv

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────
DATA_FILE = "data.json"

# Folder of per-boss emoji images, uploaded to the server and assigned to bosses
# by /seedpresets and /syncemojis. One PNG/JPG/GIF per boss, named after the
# boss: `faith.png`, `soul-lich.png`. Separators and case are ignored when
# matching, so `Soul_Lich.PNG` finds `soul-lich` too. Override with EMOJI_DIR
# in .env if you keep the images somewhere else.
EMOJI_DIR = os.environ.get("EMOJI_DIR", "emojis")

# Discord's limits for a custom emoji: the upload is rejected over 256 KB, and
# the name must be 2-32 characters of letters, digits and underscores.
EMOJI_FILE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif")
EMOJI_MAX_BYTES = 256 * 1024

# Warning rule: how far ahead of respawn the ping fires, chosen from how long
# the boss takes to come back. Anything at or under the short cutoff gets a
# 1-minute heads-up; past that, WARN_TIERS applies.
SHORT_RESPAWN_CUTOFF_MINUTES = 60
SHORT_WARN_MINUTES = 1
LONG_WARN_MINUTES = 7

# (respawn of at least this many minutes) -> warn this many minutes ahead.
# Checked longest-first, so it reads as: 6 days or more -> 60m, 2 up to 6 days
# -> 30m, 1 up to 2 days -> 15m. Anything under a day (but over the short
# cutoff) falls through to LONG_WARN_MINUTES. Bounds are inclusive at the low
# end, so a boss on exactly a 2-day timer gets the 30-minute warning.
WARN_TIERS = (
    (6 * 24 * 60, 60),
    (2 * 24 * 60, 30),
    (1 * 24 * 60, 15),
)

# Warn values the old two-step rule could produce. A boss still sitting on one
# of these has never been customised, so the tiers above are applied to it once
# (see WARN_RULE_VERSION); any other value came from `/editboss warn_minutes`
# and is left alone.
LEGACY_AUTO_WARN_MINUTES = (1, 5, 7)
WARN_RULE_VERSION = 2

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
#
# One roster per board: BOSS_PRESETS feeds the main board (`/setboard`),
# MINI_BOSS_PRESETS feeds the mini board (`/setminiboard`). Membership here is
# only the default — a boss's board is stored per guild and can be changed
# any time with `/editboss board:`.
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
    "soul-lich": 24 * 60 + 15,
    "faith":    5 * 60 + 53,
    "billiard": 7 * 60 + 55,
    "actaemon": 6 * 60,
}

MINI_BOSS_PRESETS = {
    "devilang": 5 * 60 + 33,
    "wadangka": 2 * 60 + 30,
    "awakenkooii": 1 * 60 + 3,
    "glucose":  30,
    "overload": 30,
    "apapa":    15,
}

# The two boards. Each lives in its own channel and shows only the timers for
# bosses assigned to it; warnings and respawn pings go to that same channel.
BOARDS = ("main", "mini")
BOARD_TITLES = {"main": "JR's Boss Timers", "mini": "JR's Mini Boss Timers"}
BOARD_NOUNS = {"main": "main boss", "mini": "mini boss"}
BOARD_SETTERS = {"main": "/setboard", "mini": "/setminiboard"}
PRESETS_BY_BOARD = {"main": BOSS_PRESETS, "mini": MINI_BOSS_PRESETS}

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)


# ── Persistence ───────────────────────────────────────────────────────────────
# Schema:
# {
#   "<guild_id>": {
#     "boards": {
#       "main": {"channel_id": 123, "message_id": 456},
#       "mini": {"channel_id": 789, "message_id": 111}
#     },
#     "bosses": {
#       "faith": {"respawn_minutes": 353, "warn_minutes": 7, "role_id": null,
#                 "emoji": null, "category": "main"}
#     },
#     "warn_rule": 2,
#     "timers": {
#       "faith": {"respawns_at": "2026-08-16T05:53:00+00:00", "reported_by": "someone"}
#     }
#   }
# }
# Timers stay in one flat dict; each board renders the subset whose boss carries
# its category. guild_state() migrates the old single-board keys on load.

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

# In-memory only: (guild_id, category) -> last board text actually pushed to
# Discord, used to skip redundant edits.
board_cache: dict = {}

# In-memory only: (guild_id, category) -> asyncio.Lock serialising that board.
board_locks: dict = {}


def default_category(boss_name: str) -> str:
    """Which board a boss belongs to when nothing has been stored for it yet."""
    return "mini" if boss_name in MINI_BOSS_PRESETS else "main"


def guild_state(guild_id: int) -> dict:
    key = str(guild_id)
    if key not in store:
        store[key] = {"boards": {}, "bosses": {}, "timers": {}}
    state = store[key]
    state.setdefault("bosses", {})
    state.setdefault("timers", {})
    boards = state.setdefault("boards", {})
    for category in BOARDS:
        boards.setdefault(category, {"channel_id": None, "message_id": None})

    # One live reaction-role menu per board. `map` is {emoji string: boss name},
    # keyed the way a raw reaction payload stringifies so the handler can look a
    # reaction up without the message being cached.
    menus = state.setdefault("reaction_roles", {})
    for category in BOARDS:
        menus.setdefault(category, {"channel_id": None, "message_id": None, "map": {}})

    # Migrate the old single-board schema. The existing board becomes the main
    # one and keeps its message id, so it is edited in place rather than
    # reposted — otherwise the channel ends up with two lists.
    if "board_channel_id" in state or "board_message_id" in state:
        if state.get("board_channel_id") and not boards["main"]["channel_id"]:
            boards["main"]["channel_id"] = state["board_channel_id"]
            boards["main"]["message_id"] = state.get("board_message_id")
        state.pop("board_channel_id", None)
        state.pop("board_message_id", None)

    # Bosses registered before boards existed are classified by the preset
    # rosters, so an existing install splits itself as soon as a mini board is
    # set. Anything in neither roster stays on the main board.
    for name, cfg in state["bosses"].items():
        cfg.setdefault("category", default_category(name))

    # One-time pass so a roster registered under the old two-step rule picks up
    # the tiered warning times. Only values the old rule could have produced are
    # re-derived — a boss whose warning was set by hand keeps it.
    if state.get("warn_rule") != WARN_RULE_VERSION:
        for name, cfg in state["bosses"].items():
            if cfg.get("warn_minutes") in LEGACY_AUTO_WARN_MINUTES:
                cfg["warn_minutes"] = default_warn_minutes(cfg["respawn_minutes"])
        state["warn_rule"] = WARN_RULE_VERSION

    return state


def board_state(guild_id: int, category: str) -> dict:
    return guild_state(guild_id)["boards"][category]


def boss_category(guild_id: int, boss_name: str) -> str:
    cfg = guild_state(guild_id)["bosses"].get(boss_name)
    category = cfg.get("category") if cfg else None
    return category if category in BOARDS else default_category(boss_name)


def default_warn_minutes(respawn_minutes: float) -> int:
    """How far ahead of respawn to ping, derived from the respawn length."""
    if respawn_minutes <= SHORT_RESPAWN_CUTOFF_MINUTES:
        return SHORT_WARN_MINUTES
    for min_respawn_minutes, warn_minutes in WARN_TIERS:
        if respawn_minutes >= min_respawn_minutes:
            return warn_minutes
    return LONG_WARN_MINUTES


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


def boss_label(guild_id: int, boss_name: str) -> str:
    """Display name for a boss — bold, prefixed with its emoji if one is set."""
    cfg = guild_state(guild_id)["bosses"].get(boss_name, {})
    emoji = cfg.get("emoji")
    return f"{emoji} **{boss_name.title()}**" if emoji else f"**{boss_name.title()}**"


def resolve_ping(guild: discord.Guild, boss_cfg: dict) -> str:
    """Who to ping for this boss, resolved at send time so /editboss applies live."""
    role = guild.get_role(boss_cfg["role_id"]) if boss_cfg.get("role_id") else None
    return role.mention if role else DEFAULT_PING


# ── Board messages (one updating timer list per board) ───────────────────────

def build_board_embed(guild_id: int, category: str) -> discord.Embed:
    timers = {
        name: info
        for name, info in guild_state(guild_id)["timers"].items()
        if boss_category(guild_id, name) == category
    }

    if not timers:
        return discord.Embed(
            title=BOARD_TITLES[category],
            description="No active timers :(",
            color=discord.Color.dark_grey(),
        )

    now = datetime.now(timezone.utc)
    rows = []
    for boss_name, info in sorted(timers.items(), key=lambda kv: kv[1]["respawns_at"]):
        respawns_at = datetime.fromisoformat(info["respawns_at"])
        unix_ts = int(respawns_at.timestamp())
        rows.append(
            f"{boss_label(guild_id, boss_name)} — **{format_remaining(respawns_at, now)}** "
            f"(<t:{unix_ts}:t>)"
        )

    embed = discord.Embed(
        title=BOARD_TITLES[category],
        description="\n".join(rows),
        color=discord.Color.blurple(),
    )
    embed.timestamp = now
    embed.set_footer(text="Updated")
    return embed


async def get_board_channel(guild: discord.Guild, category: str) -> discord.TextChannel | None:
    channel_id = board_state(guild.id, category)["channel_id"]
    if not channel_id:
        return None
    return guild.get_channel(channel_id)


def board_lock(guild_id: int, category: str) -> asyncio.Lock:
    key = (guild_id, category)
    lock = board_locks.get(key)
    if lock is None:
        lock = board_locks[key] = asyncio.Lock()
    return lock


async def refresh_board(guild: discord.Guild, category: str, repost: bool = False) -> None:
    """Update one board in place, or with repost=True move it to the channel bottom."""
    # Serialised per board: the refresher loop, /d's repost, a finishing timer
    # and /setboard all call this, and a repost leaves message_id None across an
    # API round trip. A second caller entering that window sees "no board yet"
    # and posts its own, leaving an orphaned list that nothing ever updates.
    # Keyed per (guild, board) so the two boards never block each other.
    async with board_lock(guild.id, category):
        board = board_state(guild.id, category)
        channel = await get_board_channel(guild, category)
        if not channel:
            return

        embed = build_board_embed(guild.id, category)
        rendered = embed.description or ""
        cache_key = (guild.id, category)

        # The countdown is minute-granular, so most ticks render identically to
        # the last one. Skip those instead of spending an API call to change
        # nothing. (Also collapses callers that queued behind a repost.)
        if not repost and board["message_id"] and board_cache.get(cache_key) == rendered:
            return

        if repost and board["message_id"]:
            # Delete the old board first — two boards in one channel means the
            # stale one keeps showing countdowns nothing is refreshing.
            try:
                old = await channel.fetch_message(board["message_id"])
                await old.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass
            board["message_id"] = None

        msg = None
        if board["message_id"]:
            try:
                msg = await channel.fetch_message(board["message_id"])
            except (discord.NotFound, discord.HTTPException):
                msg = None

        if msg is None:
            msg = await channel.send(embed=embed)
            board["message_id"] = msg.id
            save_data(store)
        else:
            try:
                await msg.edit(embed=embed)
            except discord.HTTPException:
                # Forget the cached render so the next tick retries rather than
                # assuming the board is up to date.
                board_cache.pop(cache_key, None)
                return

        board_cache[cache_key] = rendered


async def refresh_boss_board(guild: discord.Guild, boss_name: str, repost: bool = False) -> None:
    """Refresh only the board the given boss lives on."""
    await refresh_board(guild, boss_category(guild.id, boss_name), repost=repost)


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

    # Cancel any existing run for this boss, and wait for it to finish unwinding
    # before touching state. cancel() only schedules the cancellation, so the old
    # task's finally block would otherwise run *after* the write below and delete
    # the timer entry we just created — leaving the new timer invisible on the
    # board and its task dead on a KeyError.
    task_map = running_tasks.setdefault(guild.id, {})
    old_task = task_map.pop(boss_name, None)
    if old_task is not None:
        old_task.cancel()
        try:
            await old_task
        except asyncio.CancelledError:
            pass

    state["timers"][boss_name] = {
        "respawns_at": respawns_at.isoformat(),
        "reported_by": reported_by,
    }
    save_data(store)
    await refresh_boss_board(guild, boss_name, repost=repost)

    task_map[boss_name] = asyncio.create_task(_run_timer(guild, boss_name))


async def _run_timer(guild: discord.Guild, boss_name: str) -> None:
    state = guild_state(guild.id)

    try:
        timer_info = state["timers"][boss_name]
        boss_cfg = state["bosses"][boss_name]
        respawns_at = datetime.fromisoformat(timer_info["respawns_at"])
        warn_at = respawns_at - timedelta(minutes=boss_cfg["warn_minutes"])
        now = datetime.now(timezone.utc)

        unix_ts = int(respawns_at.timestamp())

        # The warning ping is kept around so the respawn can be announced by
        # editing it in place, rather than posting a second message.
        warn_msg = None

        warn_sleep = (warn_at - now).total_seconds()
        if warn_sleep > 0:
            await asyncio.sleep(warn_sleep)
            # Resolved here rather than at task start, so a boss moved between
            # boards mid-countdown alerts in its new channel.
            channel = await get_board_channel(guild, boss_category(guild.id, boss_name))
            if channel:
                warn_msg = await channel.send(
                    f"{resolve_ping(guild, boss_cfg)} {boss_label(guild.id, boss_name)} "
                    f"respawns in **{boss_cfg['warn_minutes']}m** — <t:{unix_ts}:t>"
                )

        now = datetime.now(timezone.utc)
        final_sleep = (respawns_at - now).total_seconds()
        if final_sleep > 0:
            await asyncio.sleep(final_sleep)

        respawned_text = (
            f"{resolve_ping(guild, boss_cfg)} {boss_label(guild.id, boss_name)} "
            f"has respawned at <t:{unix_ts}:t> (<t:{unix_ts}:R>)"
        )
        if warn_msg is not None:
            try:
                await warn_msg.edit(content=respawned_text)
            except discord.HTTPException:
                pass
        else:
            # No warning went out (timer started inside the warning window), so
            # there's nothing to edit — announce the respawn directly.
            channel = await get_board_channel(guild, boss_category(guild.id, boss_name))
            if channel:
                await channel.send(respawned_text)

    except asyncio.CancelledError:
        return
    finally:
        state["timers"].pop(boss_name, None)
        save_data(store)
        task_map = running_tasks.get(guild.id, {})
        task_map.pop(boss_name, None)
        await refresh_boss_board(guild, boss_name)


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


# ── Boss emoji sync ───────────────────────────────────────────────────────────
# Discord has no way to show a local image inline, so each file in EMOJI_DIR is
# uploaded once as a server custom emoji; what gets stored on the boss is the
# `<:name:id>` reference the board and pings already render.

def normalise_boss_key(text: str) -> str:
    """Fold a boss name or filename to a comparable key: letters and digits only."""
    return re.sub(r"[^a-z0-9]", "", text.lower())


def emoji_name_for(boss_name: str) -> str:
    """A Discord-legal custom emoji name: 2-32 chars of letters, digits, underscore."""
    name = re.sub(r"[^A-Za-z0-9_]", "_", boss_name)[:32]
    return name if len(name) >= 2 else f"boss_{name}"


def scan_emoji_dir() -> dict:
    """{normalised boss key: file path} for every usable image in EMOJI_DIR."""
    found = {}
    if not os.path.isdir(EMOJI_DIR):
        return found
    for entry in sorted(os.listdir(EMOJI_DIR)):
        stem, ext = os.path.splitext(entry)
        if ext.lower() not in EMOJI_FILE_EXTENSIONS:
            continue
        key = normalise_boss_key(stem)
        # First match wins, so `faith.png` beats `Faith.PNG` on a case-sensitive
        # filesystem rather than silently uploading both.
        if key and key not in found:
            found[key] = os.path.join(EMOJI_DIR, entry)
    return found


def emoji_sync_embed(guild_id: int, result: dict, seeded: list = None) -> discord.Embed:
    """Turn a sync_boss_emojis() result into something readable in Discord."""
    embed = discord.Embed(title="Boss emojis", color=discord.Color.blurple())

    if seeded:
        embed.add_field(
            name=f"Registered ({len(seeded)})",
            value=clip_names(", ".join(n.title() for n in seeded), len(seeded)),
            inline=False,
        )

    if not result["files"]:
        embed.color = discord.Color.dark_grey()
        embed.description = (
            f"No emoji images found in `{os.path.abspath(EMOJI_DIR)}`.\n"
            "Drop one PNG/JPG/GIF per boss in that folder, named after the boss "
            "(`faith.png`, `soul-lich.png`), then run this again."
        )
        return embed

    def field(title, names):
        if names:
            embed.add_field(
                name=f"{title} ({len(names)})",
                value=clip_names(", ".join(boss_label(guild_id, n) for n in names), len(names)),
                inline=False,
            )

    field("Uploaded and assigned", result["created"])
    field("Assigned from an existing server emoji", result["reused"])
    field("Left alone — already had an emoji", result["kept"])

    if result["missing"]:
        embed.add_field(
            name=f"No image file ({len(result['missing'])})",
            value=clip_names(", ".join(n.title() for n in result["missing"]),
                             len(result["missing"])),
            inline=False,
        )
    if result["failed"]:
        embed.color = discord.Color.orange()
        embed.add_field(
            name=f"Failed ({len(result['failed'])})",
            value="\n".join(f"• **{n.title()}** — {why}"
                          for n, why in result["failed"])[:1000],
            inline=False,
        )
    return embed


def clip_names(text: str, count: int, limit: int = 1000) -> str:
    """Embed fields cap at 1024 characters, so trim rather than lose the reply."""
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(",", 1)[0] + f", — ({count} total)"


async def sync_boss_emojis(guild: discord.Guild, boss_names, overwrite: bool = False) -> dict:
    """Give each named boss the server emoji built from its image file.

    Reuses an existing server emoji of the same name rather than uploading a
    duplicate, so re-running this is cheap and does not eat emoji slots.
    """
    state = guild_state(guild.id)
    files = scan_emoji_dir()
    result = {"created": [], "reused": [], "kept": [], "missing": [], "failed": [],
              "files": len(files)}

    for boss_name in boss_names:
        cfg = state["bosses"].get(boss_name)
        if cfg is None:
            continue

        path = files.get(normalise_boss_key(boss_name))
        if path is None:
            result["missing"].append(boss_name)
            continue
        if cfg.get("emoji") and not overwrite:
            result["kept"].append(boss_name)
            continue

        emoji_name = emoji_name_for(boss_name)
        existing = discord.utils.get(guild.emojis, name=emoji_name)
        if existing is not None:
            cfg["emoji"] = str(existing)
            result["reused"].append(boss_name)
            continue

        try:
            size = os.path.getsize(path)
            if size > EMOJI_MAX_BYTES:
                result["failed"].append(
                    (boss_name, f"{size // 1024} KB — over Discord's 256 KB limit")
                )
                continue
            with open(path, "rb") as fh:
                image = fh.read()
            created = await guild.create_custom_emoji(
                name=emoji_name, image=image, reason=f"Boss timer emoji for {boss_name}"
            )
        except discord.Forbidden:
            # Every remaining upload would fail identically, so stop here rather
            # than filling the report with the same line 19 times.
            result["failed"].append(
                (boss_name, "the bot is missing the **Manage Expressions** permission")
            )
            break
        except (discord.HTTPException, OSError) as exc:
            result["failed"].append((boss_name, str(exc)))
            continue

        cfg["emoji"] = str(created)
        result["created"].append(boss_name)

    save_data(store)
    return result


# ── Reaction role menu ────────────────────────────────────────────────────────
# A posted message listing bosses; reacting with a boss's emoji grants that
# boss's ping role, un-reacting takes it away. The mapping is stored so the menu
# keeps working after a restart, when the message is no longer cached.

async def ensure_ping_role(guild: discord.Guild, boss_name: str, cfg: dict,
                           create: bool = True) -> tuple:
    """The role handed out for this boss, reusing or creating one as needed.

    Returns (role, note) where note says what happened, for the admin's report.
    With create=False, returns (None, "missing") instead of making a new role.
    """
    role = guild.get_role(cfg["role_id"]) if cfg.get("role_id") else None
    if role is not None:
        return role, "existing"

    # Match an existing role by name before making another one, so re-running
    # this after clearing a role_id does not litter the server with duplicates.
    role_name = boss_name.title()
    role = discord.utils.get(guild.roles, name=role_name)
    if role is not None:
        cfg["role_id"] = role.id
        return role, "adopted"

    if not create:
        return None, "missing"

    role = await guild.create_role(
        name=role_name, mentionable=True,
        reason=f"Boss timer ping role for {boss_name}",
    )
    cfg["role_id"] = role.id
    return role, "created"


def build_reaction_role_embed(guild_id: int, category: str, entries: list) -> discord.Embed:
    lines = [
        f"{boss_label(guild_id, name)} — {role.mention}"
        for name, _emoji, role in entries
    ]
    return discord.Embed(
        title=f"{BOARD_TITLES[category]} — ping roles",
        description=(
            "React with a boss's emoji to get pinged before it respawns.\n"
            "Remove your reaction to stop being pinged for it.\n\n"
            + "\n".join(lines)
        ),
        color=discord.Color.blurple(),
    )


def reaction_role_report(guild_id: int, category: str, message, no_emoji: list,
                         no_role: list, made_roles: list) -> discord.Embed:
    """What the admin who ran /reactroles sees: what posted, and what did not."""
    embed = discord.Embed(title=f"{BOARD_NOUNS[category].title()} ping roles",
                          color=discord.Color.blurple())
    if message is None:
        embed.color = discord.Color.orange()
        embed.description = (
            f"Nothing to post — no {BOARD_NOUNS[category]} has both an emoji and a "
            f"ping role.\n Add emojis with `/syncemojis`, then run this again."
        )
    else:
        embed.description = f"Menu posted: {message.jump_url}"

    if made_roles:
        embed.add_field(
            name=f"Roles created ({len(made_roles)})",
            value=clip_names(", ".join(n.title() for n in made_roles), len(made_roles)),
            inline=False,
        )
    if no_emoji:
        embed.add_field(
            name=f"Skipped — no emoji ({len(no_emoji)})",
            value=clip_names(", ".join(n.title() for n in no_emoji), len(no_emoji))
                  + "\nGive them an image in the emoji folder and run `/syncemojis`.",
            inline=False,
        )
    if no_role:
        embed.color = discord.Color.orange()
        embed.add_field(
            name=f"Skipped — role problem ({len(no_role)})",
            value="\n".join(f"• **{n.title()}** — {why}" for n, why in no_role)[:1000],
            inline=False,
        )
    return embed


async def handle_reaction_role(payload, adding: bool) -> None:
    """Grant or remove a boss ping role from a reaction on a stored menu."""
    if payload.guild_id is None or payload.user_id == bot.user.id:
        return

    state = guild_state(payload.guild_id)
    menu = next(
        (m for m in state["reaction_roles"].values() if m.get("message_id") == payload.message_id),
        None,
    )
    if menu is None:
        return

    boss_name = menu["map"].get(str(payload.emoji))
    if boss_name is None:
        return
    cfg = state["bosses"].get(boss_name)
    if not cfg or not cfg.get("role_id"):
        return

    guild = bot.get_guild(payload.guild_id)
    if guild is None:
        return
    role = guild.get_role(cfg["role_id"])
    if role is None:
        return

    # payload.member is only populated on add, and the members intent is off, so
    # fall back to a REST fetch rather than an empty cache lookup.
    member = payload.member if adding else None
    if member is None:
        member = guild.get_member(payload.user_id)
    if member is None:
        try:
            member = await guild.fetch_member(payload.user_id)
        except discord.HTTPException:
            return
    if member.bot:
        return

    try:
        if adding:
            await member.add_roles(role, reason="Boss timer reaction role")
        else:
            await member.remove_roles(role, reason="Boss timer reaction role")
    except (discord.Forbidden, discord.HTTPException):
        pass


# ── Commands ───────────────────────────────────────────────────────────────────

async def set_board(interaction: discord.Interaction, category: str, channel: discord.TextChannel) -> None:
    """Shared by /setboard and /setminiboard."""
    other = "mini" if category == "main" else "main"
    if board_state(interaction.guild_id, other)["channel_id"] == channel.id:
        # Both boards in one channel would interleave two lists that each repost
        # to the bottom, so neither would stay visible.
        await interaction.response.send_message(
            f"{channel.mention} is already the {BOARD_NOUNS[other]} board — "
            f"pick a different channel.",
            ephemeral=True,
        )
        return

    board = board_state(interaction.guild_id, category)
    board["channel_id"] = channel.id
    board["message_id"] = None
    board_cache.pop((interaction.guild_id, category), None)
    save_data(store)

    roster = sorted(
        name for name in guild_state(interaction.guild_id)["bosses"]
        if boss_category(interaction.guild_id, name) == category
    )
    if not roster:
        detail = " — assign some with `/editboss board:` or `/seedpresets`"
    else:
        shown = ", ".join(n.title() for n in roster[:20])
        rest = len(roster) - 20
        detail = f": {shown}" + (f", and {rest} more" if rest > 0 else "")

    await interaction.response.send_message(
        f"✅ {BOARD_NOUNS[category].title()} board set to {channel.mention}.\n"
        f"It tracks **{len(roster)}** boss{'es' if len(roster) != 1 else ''}{detail}",
        ephemeral=True,
    )
    await refresh_board(interaction.guild, category)


@bot.tree.command(name="setboard", description="Set the channel for the main boss timer list and pings.")
@app_commands.describe(channel="The channel to use as the main boss timer board")
@app_commands.checks.has_permissions(manage_channels=True)
async def setboard(interaction: discord.Interaction, channel: discord.TextChannel):
    await set_board(interaction, "main", channel)


@bot.tree.command(name="setminiboard", description="Set the channel for the mini boss timer list and pings.")
@app_commands.describe(channel="The channel to use as the mini boss timer board")
@app_commands.checks.has_permissions(manage_channels=True)
async def setminiboard(interaction: discord.Interaction, channel: discord.TextChannel):
    await set_board(interaction, "mini", channel)


@bot.tree.command(name="addboss", description="Register a boss and its respawn time.")
@app_commands.describe(
    name="Boss name",
    days="Respawn days", hours="Respawn hours", minutes="Respawn minutes", seconds="Respawn seconds",
    role="Role to ping on the warning (optional)",
    warn_minutes="Override the auto warning time (optional — default scales with respawn length)",
    board="Which board this boss belongs to (default: main)",
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
    board: Literal["main", "mini"] = "main",
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
        "emoji": None,
        "category": board,
    }
    save_data(store)

    await interaction.response.send_message(
        f"✅ **{boss_name.title()}** registered.\n"
        f"• Respawn: **{format_duration(total_minutes)}**\n"
        f"• Warning ping: **{state['bosses'][boss_name]['warn_minutes']}m** before respawn\n"
        f"• Ping role: {role.mention if role else f'*(none — {DEFAULT_PING})*'}\n"
        f"• Board: **{BOARD_NOUNS[board].title()}** ({BOARD_SETTERS[board]})\n"
        f"• Emoji: *(none — set one with `/editboss`)*",
        ephemeral=True,
    )


# Words that mean "take the emoji off this boss" — slash command options can't
# express "set this back to empty" any other way.
EMOJI_CLEAR_WORDS = {"none", "no", "off", "-", "clear", "remove", "null"}


@bot.tree.command(name="editboss", description="Edit a boss's respawn time, ping role, warning time, or emoji.")
@app_commands.describe(
    boss="Boss to edit",
    days="New respawn days", hours="New respawn hours",
    minutes="New respawn minutes", seconds="New respawn seconds",
    role="New role to ping on the warning",
    emoji="Emoji shown next to this boss — pass `none` to remove it",
    warn_minutes="New warning time, in minutes before respawn",
    clear_role="Remove the ping role (falls back to @everyone)",
    board="Move this boss to the main or mini board",
)
@app_commands.autocomplete(boss=boss_autocomplete)
@app_commands.checks.has_permissions(manage_channels=True)
async def editboss(
    interaction: discord.Interaction,
    boss: str,
    days: int = None,
    hours: int = None,
    minutes: int = None,
    seconds: int = None,
    role: discord.Role = None,
    emoji: str = None,
    warn_minutes: int = None,
    clear_role: bool = False,
    board: Literal["main", "mini"] = None,
):
    state = guild_state(interaction.guild_id)
    boss = boss.lower().strip()
    # Mutated in place rather than replaced, so a timer already counting down
    # picks up the new role and emoji when it fires.
    cfg = state["bosses"].get(boss)

    if cfg is None:
        await interaction.response.send_message(
            f"**{boss}** is not registered. Use `/addboss` first.", ephemeral=True
        )
        return
    if role is not None and clear_role:
        await interaction.response.send_message(
            "Pick either `role` or `clear_role`, not both.", ephemeral=True
        )
        return

    changes = []
    timing_changed = False
    moved_from = None

    if board is not None and board != boss_category(interaction.guild_id, boss):
        moved_from = boss_category(interaction.guild_id, boss)
        cfg["category"] = board
        changes.append(
            f"• Board: **{BOARD_NOUNS[board].title()}** "
            f"(was {BOARD_NOUNS[moved_from]})"
        )

    if any(v is not None for v in (days, hours, minutes, seconds)):
        total_minutes = (days or 0) * 1440 + (hours or 0) * 60 + (minutes or 0) + (seconds or 0) / 60
        if total_minutes <= 0:
            await interaction.response.send_message(
                "Respawn time must be greater than zero.", ephemeral=True
            )
            return
        cfg["respawn_minutes"] = total_minutes
        changes.append(f"• Respawn: **{format_duration(total_minutes)}**")
        timing_changed = True

    if warn_minutes is not None:
        if warn_minutes <= 0:
            await interaction.response.send_message(
                "Warning time must be at least 1 minute.", ephemeral=True
            )
            return
        cfg["warn_minutes"] = warn_minutes
        changes.append(f"• Warning ping: **{warn_minutes}m** before respawn")
        timing_changed = True

    if clear_role:
        cfg["role_id"] = None
        changes.append(f"• Ping role: *(none — {DEFAULT_PING})*")
    elif role is not None:
        cfg["role_id"] = role.id
        changes.append(f"• Ping role: {role.mention}")

    if emoji is not None:
        new_emoji = emoji.strip()
        if new_emoji.lower() in EMOJI_CLEAR_WORDS:
            new_emoji = None
        elif len(new_emoji) > 64 or "\n" in new_emoji:
            await interaction.response.send_message(
                "That doesn't look like an emoji — pass a single emoji, or `none` to remove it.",
                ephemeral=True,
            )
            return
        cfg["emoji"] = new_emoji
        changes.append(f"• Emoji: {new_emoji}" if new_emoji else "• Emoji: *(removed)*")

    if not changes:
        await interaction.response.send_message(
            "Nothing to change — pass at least one of respawn time, `role`, `emoji`, "
            "`warn_minutes`, `clear_role`, or `board`.",
            ephemeral=True,
        )
        return

    save_data(store)

    # A running timer's respawn moment was fixed when the death was reported, so
    # a new respawn/warning length only takes effect from the next report.
    note = ""
    if timing_changed and boss in state["timers"]:
        note = (
            f"\n*{boss.title()} has a timer running — the new timing applies "
            f"from its next `/d` report.*"
        )

    await interaction.response.send_message(
        f"✅ **{boss.title()}** updated.\n" + "\n".join(changes) + note, ephemeral=True
    )
    # A move has to redraw both boards: the timer leaves one list and joins the
    # other, and only the boss's current board is refreshed by default.
    await refresh_boss_board(interaction.guild, boss)
    if moved_from is not None:
        await refresh_board(interaction.guild, moved_from)


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
    # Grouped by board, then shortest respawn first; name breaks ties so bosses
    # on the same timer keep a stable order between calls.
    lines = []
    for category in BOARDS:
        entries = [
            (name, cfg) for name, cfg in state["bosses"].items()
            if boss_category(interaction.guild_id, name) == category
        ]
        if not entries:
            continue
        if lines:
            lines.append("")
        channel_id = board_state(interaction.guild_id, category)["channel_id"]
        where = f"<#{channel_id}>" if channel_id else f"*not set — `{BOARD_SETTERS[category]}`*"
        lines.append(f"__**{BOARD_TITLES[category]}**__ — {where}")
        lines.extend(
            f"• {boss_label(interaction.guild_id, name)} — "
            f"{format_duration(cfg['respawn_minutes'])} respawn, "
            f"{cfg['warn_minutes']}m warning, pings {resolve_ping(interaction.guild, cfg)}"
            for name, cfg in sorted(entries, key=lambda kv: (kv[1]["respawn_minutes"], kv[0]))
        )

    # Sent as an embed: role mentions render as the role name but never notify
    # anyone from inside an embed, and the 4096-char description leaves room for
    # a roster whose <@&id> mentions would outgrow the 2000-char message limit.
    description = "\n".join(lines)
    if len(description) > 4000:
        kept, used = [], 0
        for line in lines:
            if used + len(line) + 1 > 3900:
                break
            kept.append(line)
            used += len(line) + 1
        description = "\n".join(kept) + f"\n*…and {len(lines) - len(kept)} more.*"

    embed = discord.Embed(
        title="Registered Bosses",
        description=description,
        color=discord.Color.blurple(),
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


# Option descriptions for /d.
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
    category = boss_category(interaction.guild_id, boss)
    if not board_state(interaction.guild_id, category)["channel_id"]:
        await interaction.response.send_message(
            f"No {BOARD_NOUNS[category]} board set yet. Ask an admin to run "
            f"`{BOARD_SETTERS[category]}`.",
            ephemeral=True,
        )
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

    channel = await get_board_channel(interaction.guild, category)
    if channel:
        await channel.send(
            f"\U0001f480 {boss_label(interaction.guild_id, boss)} reported dead {died_note} "
            f"by {interaction.user.mention}."
        )

    # Repost last so the refreshed timer list is the newest message in the channel.
    await start_timer(
        interaction.guild, boss, respawns_at, reported_by=str(interaction.user), repost=True
    )


@bot.tree.command(name="d", description="Report a boss death and start its respawn timer.")
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
    await refresh_boss_board(interaction.guild, boss)

    await interaction.response.send_message(f"\U0001f5d1️ Timer for **{boss.title()}** cancelled.")


@bot.tree.command(name="cancelall", description="Cancel every active boss timer.")
@app_commands.describe(board="Only cancel timers on this board (default: both)")
async def cancelall(interaction: discord.Interaction, board: Literal["main", "mini"] = None):
    state = guild_state(interaction.guild_id)
    task_map = running_tasks.get(interaction.guild_id, {})

    cancelled = sorted(
        name for name in state["timers"]
        if board is None or boss_category(interaction.guild_id, name) == board
    )
    if not cancelled:
        scope = "" if board is None else f" on the {BOARD_NOUNS[board]} board"
        await interaction.response.send_message(
            f"No active timers{scope} to cancel.", ephemeral=True
        )
        return

    affected = {boss_category(interaction.guild_id, name) for name in cancelled}
    summary = ", ".join(boss_label(interaction.guild_id, name) for name in cancelled)
    if len(summary) > 1500:
        summary = f"*({len(cancelled)} timers — too many to list)*"

    # Clear the stored timers before replying. That part is synchronous and
    # cannot fail, so the reply can never claim a cancellation that did not
    # happen, and the ack still lands well inside the 3-second deadline.
    for name in cancelled:
        state["timers"].pop(name, None)
    save_data(store)

    scope = "" if board is None else f" on the {BOARD_NOUNS[board]} board"
    await interaction.response.send_message(
        f"\U0001f5d1️ {interaction.user.mention} cancelled "
        f"**{len(cancelled)}** timer{'s' if len(cancelled) != 1 else ''}{scope}: {summary}"
    )

    # Every cancelled task refreshes the board as it unwinds, but only the first
    # spends an API call — the rest render an identical empty board and stop at
    # the no-op cache check.
    for name in cancelled:
        task = task_map.pop(name, None)
        if task is None:
            continue
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    for category in affected:
        await refresh_board(interaction.guild, category)


@bot.tree.command(name="seedpresets", description="Register the built-in preset bosses and set up their emojis.")
@app_commands.describe(overwrite="Replace emojis already set on a boss (default: only fill in blanks)")
@app_commands.checks.has_permissions(manage_channels=True)
async def seedpresets(interaction: discord.Interaction, overwrite: bool = False):
    # Uploading emoji images takes far longer than the 3-second interaction
    # deadline allows, so acknowledge first and report once the work is done.
    await interaction.response.defer(ephemeral=True)
    state = guild_state(interaction.guild_id)
    added = []
    for category in BOARDS:
        for name, respawn_minutes in PRESETS_BY_BOARD[category].items():
            if name not in state["bosses"]:
                state["bosses"][name] = {
                    "respawn_minutes": respawn_minutes,
                    "warn_minutes": default_warn_minutes(respawn_minutes),
                    "role_id": None,
                    "emoji": None,
                    "category": category,
                }
                added.append(name)
    save_data(store)

    # Runs over every preset boss, not just the ones just added, so an existing
    # roster picks up its emojis too.
    preset_names = [name for category in BOARDS for name in PRESETS_BY_BOARD[category]]
    result = await sync_boss_emojis(
        interaction.guild,
        [n for n in preset_names if n in state["bosses"]],
        overwrite=overwrite,
    )
    embed = emoji_sync_embed(interaction.guild_id, result, seeded=added)
    if not added:
        embed.set_footer(text="All presets were already registered.")
    await interaction.followup.send(embed=embed, ephemeral=True)

    for category in BOARDS:
        await refresh_board(interaction.guild, category)


@bot.tree.command(name="reactroles", description="Post a react-for-ping-role menu in this channel.")
@app_commands.describe(
    board="Which board's bosses to list (default: mini)",
    create_roles="Create a ping role for any boss that has none (default: yes)",
)
@app_commands.checks.has_permissions(manage_roles=True)
async def reactroles(
    interaction: discord.Interaction,
    board: Literal["main", "mini"] = "mini",
    create_roles: bool = True,
):
    # Creating roles and adding one reaction per boss is many API calls, well
    # past the 3-second interaction deadline.
    await interaction.response.defer(ephemeral=True)
    state = guild_state(interaction.guild_id)
    guild = interaction.guild

    entries, no_emoji, no_role, made_roles = [], [], [], []
    for name in sorted(n for n in state["bosses"] if boss_category(guild.id, n) == board):
        cfg = state["bosses"][name]
        # A menu entry is a reaction, so a boss with no emoji has nothing to
        # react with — skip it and say so rather than posting a blank row.
        if not cfg.get("emoji"):
            no_emoji.append(name)
            continue
        try:
            role, note = await ensure_ping_role(guild, name, cfg, create=create_roles)
        except discord.Forbidden:
            no_role.append((name, "the bot is missing the **Manage Roles** permission"))
            continue
        except discord.HTTPException as exc:
            no_role.append((name, str(exc)))
            continue
        if role is None:
            no_role.append((name, "no ping role set, and `create_roles` was off"))
            continue
        if note == "created":
            made_roles.append(name)
        entries.append((name, cfg["emoji"], role))

    save_data(store)

    if not entries:
        await interaction.followup.send(
            embed=reaction_role_report(guild.id, board, None, no_emoji, no_role, made_roles),
            ephemeral=True,
        )
        return

    # Replace any previous menu for this board, so only one message is live and
    # reactions on a stale copy cannot silently do nothing.
    menu = state["reaction_roles"][board]
    if menu.get("message_id") and menu.get("channel_id"):
        old_channel = guild.get_channel(menu["channel_id"])
        if old_channel is not None:
            try:
                old = await old_channel.fetch_message(menu["message_id"])
                await old.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

    message = await interaction.channel.send(
        embed=build_reaction_role_embed(guild.id, board, entries)
    )

    emoji_map = {}
    for name, emoji, _role in entries:
        partial = discord.PartialEmoji.from_str(emoji)
        try:
            await message.add_reaction(partial)
        except (discord.Forbidden, discord.HTTPException):
            no_role.append((name, "the bot could not add its reaction"))
            continue
        emoji_map[str(partial)] = name

    state["reaction_roles"][board] = {
        "channel_id": interaction.channel.id,
        "message_id": message.id,
        "map": emoji_map,
    }
    save_data(store)

    await interaction.followup.send(
        embed=reaction_role_report(guild.id, board, message, no_emoji, no_role, made_roles),
        ephemeral=True,
    )


@bot.tree.command(name="syncemojis", description="Upload the emoji images folder and assign them to bosses.")
@app_commands.describe(overwrite="Replace emojis already set on a boss (default: only fill in blanks)")
@app_commands.checks.has_permissions(manage_channels=True)
async def syncemojis(interaction: discord.Interaction, overwrite: bool = False):
    await interaction.response.defer(ephemeral=True)
    state = guild_state(interaction.guild_id)
    result = await sync_boss_emojis(
        interaction.guild, sorted(state["bosses"]), overwrite=overwrite
    )
    await interaction.followup.send(
        embed=emoji_sync_embed(interaction.guild_id, result), ephemeral=True
    )

    for category in BOARDS:
        await refresh_board(interaction.guild, category)


@bot.tree.command(name="help", description="How to use the boss timer bot.")
async def help_cmd(interaction: discord.Interaction):
    embed = discord.Embed(
        title="Boss Timer — Commands",
        color=discord.Color.blurple(),
    )
    embed.add_field(
        name="Everyone",
        value=(
            "`/d` — report a boss death and start its timer\n"
            "`/cancel` — cancel an active timer\n"
            "`/cancelall` — cancel every active timer (or one board's)\n"
            "`/bosses` — list registered bosses and their respawn times\n"
            "`/help` — this message"
        ),
        inline=False,
    )
    embed.add_field(
        name="Admin (Manage Channels)",
        value=(
            "`/setboard` — pick the channel for the main boss list and pings\n"
            "`/setminiboard` — pick the channel for the mini boss list and pings\n"
            "`/addboss` — register a boss\n"
            "`/editboss` — change a boss's respawn time, ping role, emoji, or board\n"
            "`/removeboss` — unregister a boss\n"
            "`/seedpresets` — register the built-in preset bosses, with emojis\n"
            f"`/syncemojis` — upload `{EMOJI_DIR}/` images and assign them to bosses\n"
            "`/reactroles` — post a react-for-ping-role menu in the current channel"
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
    embed.add_field(
        name="Boards",
        value=(
            "Two independent lists, each in its own channel:\n"
            f"• **{BOARD_TITLES['main']}** — `/setboard`\n"
            f"• **{BOARD_TITLES['mini']}** — `/setminiboard`\n"
            "A boss posts its warnings and respawn ping in its own board's "
            "channel. Move one with `/editboss board:mini`."
        ),
        inline=False,
    )
    embed.set_footer(text=f"Timer lists refresh every {BOARD_REFRESH_SECONDS}s")
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ── Bot lifecycle ─────────────────────────────────────────────────────────────

@tasks.loop(seconds=BOARD_REFRESH_SECONDS)
async def board_refresher():
    for guild in bot.guilds:
        state = guild_state(guild.id)
        for category in BOARDS:
            # Nothing counting down on this board means nothing to re-edit —
            # skip the API call.
            if not board_state(guild.id, category)["channel_id"]:
                continue
            if not any(boss_category(guild.id, name) == category for name in state["timers"]):
                continue
            try:
                await refresh_board(guild, category)
            except discord.HTTPException:
                pass


@board_refresher.before_loop
async def before_board_refresher():
    await bot.wait_until_ready()


@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    await handle_reaction_role(payload, adding=True)


@bot.event
async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent):
    await handle_reaction_role(payload, adding=False)


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
                channel = await get_board_channel(guild, boss_category(guild.id, boss_name))
                if channel:
                    unix_ts = int(respawns_at.timestamp())
                    await channel.send(
                        f"{boss_label(guild.id, boss_name)} respawned at <t:{unix_ts}:t> "
                        f"(<t:{unix_ts}:R>) while the bot was offline."
                    )
            else:
                task_map = running_tasks.setdefault(guild.id, {})
                # on_ready fires again on every gateway re-IDENTIFY. Without this
                # guard each reconnect starts a second task per boss, and the
                # warning ping fires once per copy.
                existing = task_map.get(boss_name)
                if existing is None or existing.done():
                    task_map[boss_name] = asyncio.create_task(_run_timer(guild, boss_name))
        save_data(store)
        for category in BOARDS:
            await refresh_board(guild, category)

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
