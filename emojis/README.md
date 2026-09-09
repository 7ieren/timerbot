# Boss emoji images

Drop one image per boss in this folder and run `/syncemojis` (or `/seedpresets`).
The bot uploads each one as a server custom emoji and assigns it to the matching
boss, so it shows up on the timer board and in the warning pings.

**Naming** — name the file after the boss. Case, spaces, hyphens and underscores
are ignored when matching, so all of these find the boss `soul-lich`:

    soul-lich.png    Soul_Lich.PNG    soullich.png

**Format** — `.png`, `.jpg`, `.jpeg` or `.gif`, up to **256 KB** each (Discord's
limit). Anything larger is reported and skipped. Discord displays emoji at
128x128, so there is no reason to upload anything bigger.

**Notes**

- The bot needs the **Manage Expressions** permission to upload.
- Re-running is safe: a boss that already has an emoji is left alone, and an
  existing server emoji of the same name is reused rather than re-uploaded.
- Pass `overwrite:true` to replace emojis that are already set.
- Files that match no registered boss are ignored.
- Change the folder with `EMOJI_DIR=some/other/path` in `.env`.
