# timerbot

Discord boss respawn timer bot with two independent timer boards — one for
**main bosses** and one for **mini bosses**, each in its own channel. Each
board is a single message listing that board's active timers with a live
countdown. Death reports, warning pings and respawn notices for a boss are
posted in its own board's channel.

## Setup

1. `pip install -r requirements.txt`
2. Copy `.env.example` to `.env` and fill in `DISCORD_TOKEN`.
3. Invite the bot with the `bot` and `applications.commands` scopes. It needs:
   - Send Messages, Embed Links, Manage Messages, Add Reactions — in the board channels
   - **Manage Expressions** — to upload boss emojis (`/seedpresets`, `/syncemojis`)
   - **Manage Roles** — to create and hand out ping roles (`/reactroles`)

   Administrator covers all of these. Role hierarchy still applies either way:
   the bot can only assign roles that sit **below** its own highest role.
4. `python main.py`
5. In Discord, run `/setboard` and `/setminiboard` in two different channels,
   then `/seedpresets` to register the preset bosses.

## Commands

### Everyone

- `/d boss [minute] [second] [time] [utc_offset]` — report a death and start the timer
  - no args: died just now
  - `minute:55` — died at :55 this hour
  - `minute:12 second:10` — died at 12m 10s past this hour
  - `time:14:30` / `time:23/05 14:30` — died at an exact UTC time (add `utc_offset` for local time)
- Quick report — type in either board channel (see [Quick reports](#quick-reports))
- `/cancel boss` — cancel a running timer
- `/cancelall [board]` — cancel every running timer, or only one board's
- `/bosses` — list registered bosses grouped by board, with respawn time, warning time and ping role
- `/help` — command summary

### Admin (Manage Channels)

- `/setboard #channel` — channel for the main boss board and its pings
- `/setminiboard #channel` — channel for the mini boss board and its pings (must differ from the main board)
- `/addboss name [days] [hours] [minutes] [seconds] [role] [warn_minutes] [board]` — register a boss
- `/editboss boss …` — change a boss's respawn time, warning time, ping role (`clear_role` to remove), emoji (`none` to remove), or board
- `/removeboss boss` — unregister a boss
- `/seedpresets [overwrite]` — register every boss in `BOSS_PRESETS` / `MINI_BOSS_PRESETS` and set up their emojis
- `/syncemojis [overwrite]` — upload images from the emoji folder and assign them to all registered bosses

### Admin (Manage Roles)

- `/reactroles [board] [create_roles]` — post a reaction-role menu in the current channel (default: mini bosses)

## Quick reports

Instead of `/d`, type a death straight into either board channel:

| Message | Meaning |
|---|---|
| `faith d` | died just now |
| `lich d :55`, `lich d 55` or `lich d55` | died at :55 this hour |
| `ak d 12:10`, `ak d :12:10`, `ak d 12.10` or `ak d :12.10` | died at 12 min 10 sec past this hour |

Times in a quick report are always minutes (and optionally seconds) past the
current hour — the leading colon and the space after `d` are both optional
(`apa d08`, `apa d12:10`). A minute or second that hasn't
happened yet this hour is taken as last hour. To report an exact time of day,
use `/d time:`.

- The bot reacts ✅ when the timer starts. On a mistake it reacts ❌ and replies
  with the reason; the reply deletes itself after 15 seconds.
- Either board channel works; the timer goes to the boss's own board.
- `d`, `died` and `dead` are all accepted. Messages that don't fit the pattern
  are treated as normal chat and ignored.
- Requires the **Message Content Intent** to be enabled for the bot in the
  Discord Developer Portal (Bot → Privileged Gateway Intents).

### Boss names

Anywhere a boss name is typed (`/d` or a quick report), you can use the full
name, a short name, or any unambiguous start of a name (`plata` → Platanista).
Case, spaces and hyphens don't matter. Short names are set in `BOSS_ALIASES`
at the top of `main.py`:

| Boss | Short names |
|---|---|
| Actaemon | acta, actae |
| Billiard | billi, bili, bill |
| Soul-Lich | lich |
| Barslaf | bars |
| Bigmama | bmm |
| Ukpana | ukkie, ukie |
| Darlene | witch |
| Sephia | seph |
| Caligo | cali |
| Platanista | whale |
| Apapa | apa |
| Overload | ol |
| Glucose | gluc, glu |
| Awakenkooii | ak, awaken |
| Wadangka | wdk |
| Devilang | devi |

## Boards

- `BOSS_PRESETS` feeds the main board and `MINI_BOSS_PRESETS` the mini board.
  That's only the default — move any boss with `/editboss board:main|mini`,
  even while its timer is running.
- Each board refreshes its countdown every `BOARD_REFRESH_SECONDS` (10s),
  skipping edits when nothing visible changed.
- Reporting a death reposts that board so the list stays at the bottom of the
  channel.
- When a timer is about to end, the bot posts a warning ping. At respawn it
  **edits that same message** into the respawn notice instead of posting a new one.

## How warnings work

The warning ping fires ahead of respawn based on how long the boss takes to
come back:

| Respawn | Warning |
|---|---|
| ≤ 1 hour | 1 min |
| 1 hour – 1 day | 7 min |
| 1 – 2 days | 15 min |
| 2 – 6 days | 30 min |
| 6 days or more | 60 min |

Lower bounds are inclusive, so an exactly 2-day boss gets the 30-minute warning.
Override a boss with `warn_minutes` on `/addboss` or `/editboss`. Tune the rule
at the top of `main.py` (`SHORT_RESPAWN_CUTOFF_MINUTES`, `SHORT_WARN_MINUTES`,
`LONG_WARN_MINUTES`, `WARN_TIERS`).

Warnings ping the boss's role if it has one, otherwise `@everyone`.

## Boss emojis

Emojis appear next to the boss on the board, in pings, and in `/bosses`.

1. Put one image per boss in the `emojis/` folder next to `main.py`, named
   after the boss — `faith.png`, `soul-lich.png`. Case, spaces, hyphens and
   underscores are ignored when matching (`Soul_Lich.PNG` works).
2. Run `/seedpresets` or `/syncemojis`.

- Formats: `.png`, `.jpg`, `.jpeg`, `.gif`, up to **256 KB** (Discord's limit).
- A boss that already has an emoji is left alone unless you pass `overwrite:true`.
- An existing server emoji with the same name is reused instead of re-uploaded.
- Use a different folder by setting `EMOJI_DIR` in `.env`.
- The server's emoji slot limit still applies (50 on an unboosted server).

## Reaction roles

`/reactroles` posts a menu in the channel you run it in. Members react with a
boss's emoji to get that boss's ping role, and remove the reaction to lose it.

- Bosses without an emoji are skipped, and the report lists them.
- With `create_roles` on (the default), a boss with no ping role gets one named
  after it. An existing role with that exact name (e.g. `Apapa`) is **reused**
  rather than duplicated — delete placeholder roles with those names first if
  you want fresh ones.
- Running it again replaces the previous menu for that board.
- The menu keeps working after a restart.

## Notes

- State lives in `data.json` (gitignored): boards, registered bosses, active
  timers and reaction-role menus per server. Timers resume on restart; a boss
  that respawned while the bot was offline gets a notice instead.
- `data.json` and `emojis/` are relative to the working directory the bot is
  started from, so start it from this folder.
- Data from older versions (single board, old warning times) is migrated
  automatically on load. Bosses still on an old default warning time get the
  new tiers; custom warning times are kept.
- Single file, no cogs.
