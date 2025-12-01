import os
import json
import time
import requests
import logging
from typing import Optional, List, Dict
from datetime import datetime, timezone, time as dtime, timedelta
from requests.exceptions import HTTPError

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

from cryptography.fernet import Fernet


# ========== PATHS & VAULT FILES ==========

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
VAULT_FILE = os.path.join(SCRIPT_DIR, "vault.bin")
KEY_FILE = os.path.join(SCRIPT_DIR, "vault.key")
LOG_FILE = os.path.join(SCRIPT_DIR, "boxcast_download.log")
STATE_FILE = os.path.join(SCRIPT_DIR, "boxcast_state.json")


def load_secrets_from_vault() -> dict:
    """
    Decrypts vault.bin using vault.key and returns:
      - client_id
      - client_secret
      - discord_webhook
    """
    if not os.path.exists(KEY_FILE):
        raise RuntimeError(f"Vault key file not found: {KEY_FILE}")
    if not os.path.exists(VAULT_FILE):
        raise RuntimeError(f"Vault file not found: {VAULT_FILE}")

    with open(KEY_FILE, "rb") as f:
        key = f.read().strip()
    fernet = Fernet(key)

    with open(VAULT_FILE, "rb") as f:
        token = f.read()

    data = fernet.decrypt(token)
    secrets = json.loads(data.decode("utf-8"))

    required = ["client_id", "client_secret", "discord_webhook"]
    for r in required:
        if r not in secrets or not secrets[r]:
            raise RuntimeError(f"Missing '{r}' in decrypted vault data")

    return secrets


# ========== CONFIG ==========

# Load encrypted secrets
_secrets = load_secrets_from_vault()

CLIENT_ID = _secrets["client_id"]
CLIENT_SECRET = _secrets["client_secret"]
DISCORD_WEBHOOK = _secrets["discord_webhook"]

# Change this to wherever you want downloads to land.
# On Ubuntu/RasPi you might mount your NAS at /mnt/boxcast.
BASE_DIR = r"C:\Users\zacpi\Downloads\test"

AUTH_URL = "https://rest.boxcast.com/oauth2/token"
API_BASE = "https://rest.boxcast.com"

# Only process broadcasts that start on/after this date (UTC)
START_DATE = datetime(2025, 11, 30, 0, 0, 0, tzinfo=timezone.utc)

# Your time zone
LOCAL_TZ = ZoneInfo("America/Chicago")

# Seconds between polling a recording's status
POLL_INTERVAL = 30

# Discord on/off
USE_DISCORD_NOTIFICATIONS = True


# ========== LOGGING SETUP ==========

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s: %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler()
    ],
)


# ========== STATE HELPERS ==========

def load_state() -> Dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if not isinstance(data, dict):
                return {}
            return data
    except FileNotFoundError:
        return {}
    except Exception as e:
        logging.error("Error loading state file: %s", e)
        return {}


def save_state(state: Dict):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        logging.error("Error saving state file: %s", e)


# ========== BASIC HELPERS ==========

def get_token() -> str:
    logging.info("Requesting BoxCast OAuth token...")
    resp = requests.post(
        AUTH_URL,
        auth=(CLIENT_ID, CLIENT_SECRET),
        data={"grant_type": "client_credentials"},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    logging.info(
        "Access token obtained (scope: %s, expires_in: %s)",
        data.get("scope"), data.get("expires_in")
    )
    return data["access_token"]


def api_get(path: str, token: str, params=None):
    resp = requests.get(
        API_BASE + path,
        headers={"Authorization": f"Bearer {token}"},
        params=params or {},
        timeout=30,
    )
    resp.raise_for_status()
    return resp


def api_post(path: str, token: str, json=None):
    resp = requests.post(
        API_BASE + path,
        headers={"Authorization": f"Bearer {token}"},
        json=json or {},
        timeout=30,
    )
    resp.raise_for_status()
    return resp


def interval_overlaps(a_start, a_end, b_start, b_end):
    return a_start < b_end and a_end > b_start


def make_safe_filename(name: str) -> str:
    invalid = '<>:"/\\|?*'
    safe = "".join("_" if c in invalid else c for c in name)
    return " ".join(safe.split()).strip()


def detect_holiday(name_lower: str) -> Optional[str]:
    if "easter" in name_lower:
        return "Easter"
    if "thanksgiving eve" in name_lower:
        return "Thanksgiving Eve"
    if "christmas eve" in name_lower:
        return "Christmas Eve"
    if "good friday" in name_lower:
        return "Good Friday"
    if "new year" in name_lower:
        return "New Year"
    return None


# ========== DISCORD HELPERS ==========

def discord_post(content: str):
    if not USE_DISCORD_NOTIFICATIONS:
        return
    if not DISCORD_WEBHOOK:
        logging.error("Discord webhook URL not configured.")
        return
    try:
        resp = requests.post(DISCORD_WEBHOOK, json={"content": content}, timeout=10)
        if resp.status_code >= 400:
            logging.error(
                "Failed to send Discord message (%s): %s",
                resp.status_code,
                resp.text[:200],
            )
    except Exception as e:
        logging.error("Error sending Discord message: %s", e)


def send_notification(subject: str, body: str):
    """
    Sends a single notification (e.g., for uncategorized, missing schedule, etc.).
    """
    logging.warning("NOTIFICATION: %s -- %s", subject, body)
    content = f"**{subject}**\n{body}"
    discord_post(content)


def send_run_summary(downloads: List[Dict]):
    """
    Sends a Discord summary at the end of each run with all downloads.
    Each entry in `downloads`:
      { "name": ..., "category": ..., "path": ... }
    """
    total = len(downloads)
    logging.info("Preparing run summary for %d downloads", total)

    if total == 0:
        content = "**BoxCast Download Summary**\nNo new services were downloaded this run."
    else:
        lines = ["**BoxCast Download Summary**", f"Downloads this run: {total}", ""]
        for d in downloads:
            lines.append(f"- `{d['name']}` → `{d['path']}` (category: {d['category']})")
        content = "\n".join(lines)

    discord_post(content)


# ========== SUNDAY ROUTING HELPERS ==========

def pick_sunday_folder_and_filename(
    starts_at_utc: datetime,
    ends_at_utc: Optional[datetime]
):
    """
    Sunday time-window routing:
    - <10:00        -> 1st Service
    - 10:00–10:50   -> Sunday School
    - 10:50–13:00   -> 2nd Service
    """
    local_start = starts_at_utc.astimezone(LOCAL_TZ)

    if ends_at_utc:
        local_end = ends_at_utc.astimezone(LOCAL_TZ)
    else:
        local_end = local_start + timedelta(hours=2)

    filename = f"{local_start:%Y-%m-%d}.mp4"
    subfolder = None

    if local_start.weekday() == 6:  # Sunday
        day = local_start.date()

        w1_start = datetime.combine(day, dtime(0, 0), tzinfo=LOCAL_TZ)
        w1_end = datetime.combine(day, dtime(10, 0), tzinfo=LOCAL_TZ)

        w2_start = datetime.combine(day, dtime(10, 0), tzinfo=LOCAL_TZ)
        w2_end = datetime.combine(day, dtime(10, 50), tzinfo=LOCAL_TZ)

        w3_start = datetime.combine(day, dtime(10, 50), tzinfo=LOCAL_TZ)
        w3_end = datetime.combine(day, dtime(13, 0), tzinfo=LOCAL_TZ)

        if interval_overlaps(local_start, local_end, w1_start, w1_end):
            subfolder = "1st Service"
        elif interval_overlaps(local_start, local_end, w2_start, w2_end):
            subfolder = "Sunday School"
        elif interval_overlaps(local_start, local_end, w3_start, w3_end):
            subfolder = "2nd Service"

    if subfolder:
        dest_dir = os.path.join(BASE_DIR, subfolder)
    else:
        dest_dir = BASE_DIR

    os.makedirs(dest_dir, exist_ok=True)
    return dest_dir, filename, subfolder


def compute_christmas_at_carbondale_filename(dest_dir: str, year: int) -> str:
    base = f"{year} Christmas At Carbondale"
    first_path = os.path.join(dest_dir, base + ".mp4")
    if not os.path.exists(first_path):
        return base + ".mp4"

    existing = [
        f for f in os.listdir(dest_dir)
        if f.startswith(base) and f.lower().endswith(".mp4")
    ]
    num_services = len(existing)
    next_index = num_services + 1
    return f"{base} Service {next_index}.mp4"


# ========== LIVE STREAM MONITORING ==========

def monitor_live_streams(token: str, state: Dict) -> Dict:
    """
    Sends alerts when streams start or end.
    Uses state["live_ids"] to track previous run.
    """
    prev_live_ids = set(state.get("live_ids", []))

    params = {
        "filter.is_live": "true",
        "l": "100",
        "s": "starts_at",
    }
    try:
        resp = api_get("/account/broadcasts", token, params=params)
        live_broadcasts = resp.json()
    except Exception as e:
        logging.error("Error fetching live broadcasts: %s", e)
        live_broadcasts = []

    current_live_ids = {b["id"] for b in live_broadcasts}

    # New live streams (start alerts)
    for b in live_broadcasts:
        bid = b["id"]
        if bid not in prev_live_ids:
            name = b.get("name", "")
            starts_at_utc = datetime.fromisoformat(
                b["starts_at"].replace("Z", "+00:00")
            )
            local_start = starts_at_utc.astimezone(LOCAL_TZ)
            send_notification(
                subject="BoxCast Live Stream Started",
                body=f"'{name}' just went live.\nStart time (local): {local_start}"
            )

    # Streams that ended since last run (end alerts)
    ended_ids = prev_live_ids - current_live_ids
    for bid in ended_ids:
        try:
            detail = api_get(f"/account/broadcasts/{bid}", token).json()
            name = detail.get("name", "")
            stops_at = detail.get("stops_at")
            if stops_at:
                ended_utc = datetime.fromisoformat(stops_at.replace("Z", "+00:00"))
                local_end = ended_utc.astimezone(LOCAL_TZ)
                end_info = f"End time (local): {local_end}"
            else:
                end_info = "End time unknown (no stops_at in API)."

            send_notification(
                subject="BoxCast Live Stream Ended",
                body=f"'{name}' is no longer live.\n{end_info}"
            )
        except Exception as e:
            logging.error("Error fetching details for ended live stream %s: %s", bid, e)

    state["live_ids"] = list(current_live_ids)
    return state


# ========== FUTURE SCHEDULE CHECK (NEXT 7 DAYS) ==========

def check_expected_schedule(token: str, state: Dict) -> Dict:
    """
    Look ahead 7 days and ensure we have:
      - Sunday 1st Service
      - Sunday 2nd Service
      - Wednesday Night
    by time window, not by title.
    If missing, send a Discord alert once per day.
    """
    today_local = datetime.now(LOCAL_TZ).date()
    today_str = today_local.isoformat()

    if state.get("last_schedule_check_date") == today_str:
        return state  # already checked today

    start_local = datetime.combine(today_local, dtime(0, 0), tzinfo=LOCAL_TZ)
    end_local = start_local + timedelta(days=7)
    start_utc = start_local.astimezone(timezone.utc)
    end_utc = end_local.astimezone(timezone.utc)

    start_iso = start_utc.isoformat().replace("+00:00", "Z")
    end_iso = end_utc.isoformat().replace("+00:00", "Z")
    q = f"starts_at:[{start_iso} TO {end_iso}]"

    params = {
        "q": q,
        "s": "starts_at",
        "l": "200",
    }

    try:
        resp = api_get("/account/broadcasts", token, params=params)
        future_broadcasts = resp.json()
    except Exception as e:
        logging.error("Error fetching future broadcasts for schedule check: %s", e)
        future_broadcasts = []

    intervals = []
    for b in future_broadcasts:
        starts_at_utc = datetime.fromisoformat(b["starts_at"].replace("Z", "+00:00"))
        local_start = starts_at_utc.astimezone(LOCAL_TZ)
        local_end = local_start + timedelta(hours=2)  # assume 2h duration
        intervals.append((local_start, local_end, b))

    missing_slots = []

    for i in range(7):
        day = today_local + timedelta(days=i)
        wday = day.weekday()  # Mon=0 ... Sun=6

        # Sunday: expect 1st and 2nd service
        if wday == 6:
            s1_start = datetime.combine(day, dtime(0, 0), tzinfo=LOCAL_TZ)
            s1_end = datetime.combine(day, dtime(10, 0), tzinfo=LOCAL_TZ)

            s2_start = datetime.combine(day, dtime(10, 50), tzinfo=LOCAL_TZ)
            s2_end = datetime.combine(day, dtime(13, 0), tzinfo=LOCAL_TZ)

            if not any(interval_overlaps(ls, le, s1_start, s1_end) for ls, le, _ in intervals):
                missing_slots.append(
                    f"{day} (Sunday) — 1st Service window (before 10:00)"
                )

            if not any(interval_overlaps(ls, le, s2_start, s2_end) for ls, le, _ in intervals):
                missing_slots.append(
                    f"{day} (Sunday) — 2nd Service window (10:50–13:00)"
                )

        # Wednesday: expect Wednesday night 18:00–21:00
        if wday == 2:
            w_start = datetime.combine(day, dtime(18, 0), tzinfo=LOCAL_TZ)
            w_end = datetime.combine(day, dtime(21, 0), tzinfo=LOCAL_TZ)

            if not any(interval_overlaps(ls, le, w_start, w_end) for ls, le, _ in intervals):
                missing_slots.append(
                    f"{day} (Wednesday) — Wednesday Night window (18:00–21:00)"
                )

    if missing_slots:
        body_lines = [
            "The following expected BoxCast streams are NOT scheduled in the next 7 days:",
            "",
        ] + [f"- {slot}" for slot in missing_slots]
        send_notification(
            subject="Missing Scheduled BoxCast Streams (Next 7 Days)",
            body="\n".join(body_lines),
        )
    else:
        logging.info("All expected Sunday/Wed slots found in next 7 days.")

    state["last_schedule_check_date"] = today_str
    return state


# ========== WEEKLY ANALYTICS (PAST 7 DAYS) ==========

def weekly_analytics(token: str, state: Dict) -> Dict:
    """
    Once per week (Monday), look back at the previous 7 days and summarize:
      - Sunday 1st/2nd Service
      - Wednesday Night
      - Holiday, Memorial, Christmas at Carbondale, Youth, Other
    For each: scheduled count + how many have recordings.
    Sends a Discord summary.
    """
    today_local = datetime.now(LOCAL_TZ).date()
    # Only run on Monday (0)
    if today_local.weekday() != 0:
        return state

    today_str = today_local.isoformat()
    if state.get("last_analytics_date") == today_str:
        return state  # already ran weekly summary today

    # Last week: Monday 00:00 to this Monday 00:00
    end_local = datetime.combine(today_local, dtime(0, 0), tzinfo=LOCAL_TZ)
    start_local = end_local - timedelta(days=7)

    start_utc = start_local.astimezone(timezone.utc)
    end_utc = end_local.astimezone(timezone.utc)

    start_iso = start_utc.isoformat().replace("+00:00", "Z")
    end_iso = end_utc.isoformat().replace("+00:00", "Z")
    q = f"starts_at:[{start_iso} TO {end_iso}]"

    params = {
        "q": q,
        "s": "starts_at",
        "l": "500",
    }

    try:
        resp = api_get("/account/broadcasts", token, params=params)
        past_broadcasts = resp.json()
    except Exception as e:
        logging.error("Error fetching broadcasts for weekly analytics: %s", e)
        past_broadcasts = []

    # category -> {scheduled, recordings}
    stats = {
        "sunday_1st": {"scheduled": 0, "recordings": 0},
        "sunday_2nd": {"scheduled": 0, "recordings": 0},
        "wednesday": {"scheduled": 0, "recordings": 0},
        "holiday": {"scheduled": 0, "recordings": 0},
        "memorial": {"scheduled": 0, "recordings": 0},
        "christmas_at_carbondale": {"scheduled": 0, "recordings": 0},
        "youth": {"scheduled": 0, "recordings": 0},
        "other": {"scheduled": 0, "recordings": 0},
    }

    for b in past_broadcasts:
        name = b.get("name", "")
        name_lower = name.lower()
        has_rec = bool(b.get("has_recording"))

        starts_at_utc = datetime.fromisoformat(b["starts_at"].replace("Z", "+00:00"))
        local_start = starts_at_utc.astimezone(LOCAL_TZ)
        local_end = local_start + timedelta(hours=2)  # assume 2h duration
        day = local_start.date()
        wday = day.weekday()

        # classify
        if "youth service" in name_lower:
            cat = "youth"
        elif "memorial" in name_lower:
            cat = "memorial"
        elif "christmas at carbondale" in name_lower:
            cat = "christmas_at_carbondale"
        else:
            holiday = detect_holiday(name_lower)
            if holiday:
                cat = "holiday"
            else:
                # Sunday windows
                if wday == 6:
                    s1_start = datetime.combine(day, dtime(0, 0), tzinfo=LOCAL_TZ)
                    s1_end = datetime.combine(day, dtime(10, 0), tzinfo=LOCAL_TZ)

                    s2_start = datetime.combine(day, dtime(10, 50), tzinfo=LOCAL_TZ)
                    s2_end = datetime.combine(day, dtime(13, 0), tzinfo=LOCAL_TZ)

                    if interval_overlaps(local_start, local_end, s1_start, s1_end):
                        cat = "sunday_1st"
                    elif interval_overlaps(local_start, local_end, s2_start, s2_end):
                        cat = "sunday_2nd"
                    else:
                        cat = "other"
                # Wednesday night window: 18:00–21:00
                elif wday == 2:
                    w_start = datetime.combine(day, dtime(18, 0), tzinfo=LOCAL_TZ)
                    w_end = datetime.combine(day, dtime(21, 0), tzinfo=LOCAL_TZ)
                    if interval_overlaps(local_start, local_end, w_start, w_end):
                        cat = "wednesday"
                    else:
                        cat = "other"
                else:
                    cat = "other"

        stats[cat]["scheduled"] += 1
        if has_rec:
            stats[cat]["recordings"] += 1

    week_start_display = start_local.date()
    week_end_display = (end_local - timedelta(days=1)).date()

    lines = [
        f"**Weekly BoxCast Summary**",
        f"Period: {week_start_display} to {week_end_display}",
        "",
        f"- Sunday 1st Service: scheduled {stats['sunday_1st']['scheduled']}, recordings {stats['sunday_1st']['recordings']}",
        f"- Sunday 2nd Service: scheduled {stats['sunday_2nd']['scheduled']}, recordings {stats['sunday_2nd']['recordings']}",
        f"- Wednesday Night: scheduled {stats['wednesday']['scheduled']}, recordings {stats['wednesday']['recordings']}",
        "",
        f"- Holiday Services: scheduled {stats['holiday']['scheduled']}, recordings {stats['holiday']['recordings']}",
        f"- Memorial Services: scheduled {stats['memorial']['scheduled']}, recordings {stats['memorial']['recordings']}",
        f"- Christmas at Carbondale: scheduled {stats['christmas_at_carbondale']['scheduled']}, recordings {stats['christmas_at_carbondale']['recordings']}",
        "",
        f"- Youth Services: scheduled {stats['youth']['scheduled']}, recordings {stats['youth']['recordings']}",
        f"- Other: scheduled {stats['other']['scheduled']}, recordings {stats['other']['recordings']}",
    ]

    discord_post("\n".join(lines))

    state["last_analytics_date"] = today_str
    return state


# ========== MAIN SCRIPT ==========

def main():
    logging.info("========== BoxCast Auto Downloader Started ==========")
    logging.info("Local download directory: %s", BASE_DIR)
    logging.info("Filtering broadcasts starting on/after %s (UTC)", START_DATE)

    state = load_state()
    token = get_token()

    # Live stream monitoring (start/end alerts)
    state = monitor_live_streams(token, state)

    # Future-week schedule check (once per day)
    state = check_expected_schedule(token, state)

    # Weekly analytics (once per week, on Monday)
    state = weekly_analytics(token, state)

    download_count = 0
    downloads_info: List[Dict] = []

    # Normal download logic
    start_iso = START_DATE.isoformat().replace("+00:00", "Z")
    date_range_query = f"starts_at:[{start_iso} TO 9999-12-31T23:59:59Z]"

    params = {
        "filter.has_recording": "true",
        "q": date_range_query,
        "s": "starts_at",
        "l": "100",
    }

    resp = api_get("/account/broadcasts", token, params=params)
    broadcasts = resp.json()
    logging.info("Found %d broadcasts with recordings in date range", len(broadcasts))

    for b in broadcasts:
        bid = b["id"]
        name = b.get("name", "")
        name_lower = name.lower()

        starts_at_utc = datetime.fromisoformat(b["starts_at"].replace("Z", "+00:00"))
        stops_at = b.get("stops_at")
        ends_at_utc = (
            datetime.fromisoformat(stops_at.replace("Z", "+00:00"))
            if stops_at else None
        )

        if starts_at_utc < START_DATE:
            continue

        # Skip Youth Service
        if "youth service" in name_lower:
            logging.info("Skipping Youth Service broadcast: %s", name)
            continue

        dest_dir, filename, sunday_subfolder = pick_sunday_folder_and_filename(
            starts_at_utc, ends_at_utc
        )

        special_category = "normal"

        # Memorial services
        if "memorial" in name_lower:
            special_category = "memorial"
            dest_dir = os.path.join(BASE_DIR, "Memorial Services")
            os.makedirs(dest_dir, exist_ok=True)
            filename = make_safe_filename(name) + ".mp4"

        # Christmas at Carbondale
        elif "christmas at carbondale" in name_lower:
            special_category = "christmas_at_carbondale"
            dest_dir = os.path.join(BASE_DIR, "Christmas At Carbondale")
            os.makedirs(dest_dir, exist_ok=True)
            local_start = starts_at_utc.astimezone(LOCAL_TZ)
            year = local_start.year
            filename = compute_christmas_at_carbondale_filename(dest_dir, year)

        else:
            # Holiday services
            holiday = detect_holiday(name_lower)
            if holiday:
                special_category = "holiday"
                dest_dir = os.path.join(BASE_DIR, "Holiday Services")
                os.makedirs(dest_dir, exist_ok=True)
                local_start = starts_at_utc.astimezone(LOCAL_TZ)
                year = local_start.year
                filename = f"{year} {holiday}.mp4"

        # Uncategorized (no Sunday slot + no special category)
        if sunday_subfolder is None and special_category == "normal":
            special_category = "uncategorized"
            dest_dir = os.path.join(BASE_DIR, "Uncategorized")
            os.makedirs(dest_dir, exist_ok=True)
            local_start = starts_at_utc.astimezone(LOCAL_TZ)
            base_name = f"{local_start:%Y-%m-%d} - {name}"
            filename = make_safe_filename(base_name) + ".mp4"

            send_notification(
                subject="Uncategorized BoxCast Service Detected",
                body=(
                    f"Broadcast '{name}' (ID: {bid}) did not match any known rules.\n"
                    f"Starts at (local): {local_start}\n"
                    f"Placing in: {dest_dir}\n"
                    f"Filename: {filename}"
                )
            )

        outfile = os.path.join(dest_dir, filename)
        logging.info("Broadcast: %s | category=%s -> %s",
                     name, special_category, outfile)

        if os.path.exists(outfile):
            logging.info("Already exists, skipping.")
            continue

        detail_resp = api_get(f"/account/broadcasts/{bid}", token)
        detail = detail_resp.json()
        recording_id = detail.get("recording_id")

        if not recording_id:
            logging.warning("No recording_id for %s", name)
            continue

        logging.info("Requesting export for recording %s", recording_id)
        try:
            api_post(f"/account/recordings/{recording_id}/download", token)
        except HTTPError as e:
            if e.response is not None and e.response.status_code == 409:
                logging.info("Export already requested (409 Conflict). Continuing.")
            else:
                logging.error("Export request failed: %s", e)
                continue

        # Poll until ready
        while True:
            rec = api_get(f"/account/recordings/{recording_id}", token)
            rec_data = rec.json()
            status = rec_data.get("download_status", "")

            logging.info("Recording %s status: %s", recording_id, status)

            if status == "ready":
                url = rec_data["download_url"]
                logging.info("Downloading %s -> %s", url, outfile)

                with requests.get(url, stream=True, timeout=600) as r2:
                    r2.raise_for_status()
                    with open(outfile, "wb") as f:
                        for chunk in r2.iter_content(1024 * 1024):
                            if chunk:
                                f.write(chunk)

                logging.info("✔ Download complete: %s", outfile)
                download_count += 1

                downloads_info.append({
                    "name": name,
                    "category": special_category if special_category != "normal" else (sunday_subfolder or "normal"),
                    "path": outfile,
                })

                break

            elif status.startswith("failed"):
                logging.error("Download failed: %s", status)
                break

            else:
                time.sleep(POLL_INTERVAL)

    logging.info("========== BoxCast Auto Downloader Finished ==========")
    logging.info("Downloads this run: %d", download_count)

    # Run summary to Discord
    send_run_summary(downloads_info)

    # Save updated state
    save_state(state)

    print("\nDownloads this run:", download_count)
    print("Log file:", LOG_FILE)
    print("State file:", STATE_FILE)


if __name__ == "__main__":
    main()
