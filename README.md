# BoxCast Auto Downloader & Discord Monitor

Python automation that:

- Downloads BoxCast service recordings after they’re ready
- Sorts and renames files into service-specific folders
- Sends Discord notifications for:
  - Live stream start / live stream end
  - Missing expected streams in the next 7 days
  - Uncategorized broadcasts
  - Weekly analytics (past 7 days)
  - Per-run download summary

Designed for church workflows and to run headless on a Raspberry Pi / Ubuntu server, but also works on Windows.

---

## Features

### 🎥 Auto Download & Sorting

- Uses the BoxCast API (client credentials flow) to:
  - Find broadcasts with recordings from `START_DATE` onward
  - Request a download/export for each recording
  - Poll until the download is ready
  - Download the MP4 and store it under a base directory

- Sunday routing (based on **when the broadcast is active**, not just start time):
  - Before 10:00 → `1st Service`
  - 10:00–10:50 → `Sunday School`
  - 10:50–13:00 → `2nd Service`

- Special cases:
  - **Youth Service** → skipped entirely
  - Titles containing **"Memorial"** → `Memorial Services/<full title>.mp4`
  - Holiday titles: `Easter`, `Thanksgiving Eve`, `Christmas Eve`, `Good Friday`, `New Year`
    - Saved under `Holiday Services/<year> <Holiday>.mp4`
  - Titles containing **"Christmas at Carbondale"**:
    - Saved under `Christmas At Carbondale/<year> Christmas At Carbondale.mp4`
    - If multiple in the same year, additional ones get
      `... Service 2`, `... Service 3`, etc.
  - Anything that doesn’t match a Sunday window or special case:
    - Saved under `Uncategorized/<YYYY-MM-DD> - <Title>.mp4`
    - Triggers a Discord notification so you can manually review it

### 🔔 Discord Notifications

Uses a single Discord webhook (stored encrypted) to send:

- **Per-run summary**:
  - How many broadcasts were downloaded
  - For each: title, category, and file path

- **Uncategorized broadcast alert**:
  - Fires whenever a broadcast doesn’t fit Sunday windows or special categories

- **Live monitoring**:
  - Start alert when a BoxCast broadcast becomes live
  - End alert when it is no longer live  
    (tracks state in `boxcast_state.json` to avoid duplicate spam)

- **Schedule health (next 7 days)**:
  - Once per day, looks ahead 7 days and checks for:
    - Sunday 1st Service (pre-10:00)
    - Sunday 2nd Service (10:50–13:00)
    - Wednesday Night (18:00–21:00)
  - If any expected slot is missing, sends a Discord warning

- **Weekly analytics (past 7 days)**:
  - Runs on **Monday**
  - Groups broadcasts into:
    - Sunday 1st / Sunday 2nd / Wednesday Night
    - Holiday / Memorial / Christmas at Carbondale / Youth / Other
  - Reports:
    - How many were scheduled
    - How many actually have recordings
  - Posts a summary message to Discord

### 🔐 Encrypted Secrets (“Vault”)

Instead of hard-coding secrets, the project uses a simple vault mechanism:

- `create_vault.py` asks for:
  - `CLIENT_ID`
  - `CLIENT_SECRET`
  - `DISCORD_WEBHOOK` URL
- Encrypts them with a randomly generated symmetric key
- Saves:
  - `vault.bin` – encrypted blob
  - `vault.key` – symmetric key used at runtime

The main script decrypts at startup and never stores secrets in plain text.

> **Important:** `vault.key` should **not** be committed to Git and should have restricted permissions (e.g. `chmod 600 vault.key` on Linux).

---

## Requirements

- Python 3.9+ recommended
- Libraries:
  - `requests`
  - `cryptography`
  - `backports.zoneinfo` (only needed on Python < 3.9)

Install:

```bash
python3 -m pip install requests cryptography backports.zoneinfo
