# timerbot

Single-channel boss respawn timer bot. One message in one channel lists every
active timer; it's empty (just the title) when nothing is running. Death
reports and warning/respawn pings are posted as separate messages in the same
channel so you get a log of activity alongside the live list.

## Setup

1. `pip install -r requirements.txt`
2. Copy `.env.example` to `.env` and fill in `DISCORD_TOKEN`.
3. Invite the bot with the `applications.commands` and `bot` scopes, with
   Send Messages / Embed Links / Manage Messages permissions in the target
   channel.
4. `python main.py`

## Commands

- `/setboard #channel` — designate the channel for the timer list + notifications (admin)
- `/addboss name days hours minutes [role] [warn_minutes]` — register a boss (admin)
- `/seedpresets` — bulk-add a few example bosses from `BOSS_PRESETS` (admin)
- `/removeboss boss` — unregister a boss (admin)
- `/bosses` — list registered bosses and their respawn/warning times
- `/died boss [minute] [time] [utc_offset]` — report a death and start the timer
  - no args: died just now
  - `minute:55` — died at :55 this hour
  - `time:14:30` / `time:23/05 14:30` — died at an exact UTC time (add `utc_offset` for local time)
- `/cancel boss` — cancel a running timer
- `/editboss` — edit a boss' respawn/warning times, roles to ping, and emoji

## How warnings work

Each boss gets an automatic warning ping before it respawns:
- respawn ≤ 60 min → **1-minute** warning
- respawn > 60 min → **5-minute** warning

Override per-boss with `warn_minutes` on `/addboss`. Adjust the cutoff/values
at the top of `main.py` (`SHORT_RESPAWN_CUTOFF_MINUTES`, `SHORT_WARN_MINUTES`,
`LONG_WARN_MINUTES`) if you want a different rule.

## Notes on this draft

- State lives in `data.json` (gitignored) — one entry per guild with
  registered bosses and active timers. Timers resume automatically on restart.
- The board message uses Discord's relative timestamps (`<t:...:R>`), so the
  countdown updates live client-side without the bot re-editing every minute.
  The bot only edits the board when a timer starts, ends, or is cancelled.
- No boss preset list is assumed beyond the small `BOSS_PRESETS` seed — add
  your game's real bosses with `/addboss` (or edit the seed dict).
- Single-file, no cog structure, no reaction roles / battlefield reminders —
  intentionally minimal since this is a first draft to react to.
