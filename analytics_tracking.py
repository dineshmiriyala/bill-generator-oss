# analytics_tracker.py
import json
import os
import sys
import time
from datetime import datetime, date, timezone
from logging import exception
from pathlib import Path
import structlog
import requests

basedir = Path(__file__).parent.resolve()

LOG_RETENTION_DAYS = 30
_LOGS_PRUNED = False


def normalize_timestamp(ts: str) -> str:
    return ts.replace("T", " ").replace("Z", "+00")

def _desktop_data_dir(app_name: str) -> Path:
    if os.name == "nt":
        return Path(os.getenv("APPDATA", str(Path.home() / "AppData" / "Roaming"))) / app_name
    elif sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / app_name
    else:
        return Path.home() / ".local" / "share" / app_name


def _prune_old_logs(log_dir: Path) -> None:
    """Delete analytics log files older than LOG_RETENTION_DAYS. Runs once per process."""
    global _LOGS_PRUNED
    if _LOGS_PRUNED:
        return
    _LOGS_PRUNED = True
    cutoff = time.time() - (LOG_RETENTION_DAYS * 86400)
    try:
        for path in log_dir.glob("analytics_*.json"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError as exc:
                print(f"[warn] could not remove old analytics log {path}: {exc}")
    except OSError as exc:
        print(f"[warn] could not enumerate analytics logs in {log_dir}: {exc}")


def log_user_event(data):
    """Append analytics event data to daily analytics log file."""
    APP_NAME = os.getenv("BG_APP_NAME", "Bill Generator")
    is_desktop = os.getenv("BG_DESKTOP") == "1"
    today_str = datetime.now().strftime("%Y_%m_%d")
    date_dir_str = datetime.now().strftime("%d-%m-%Y")

    if is_desktop:
        data_dir = _desktop_data_dir(APP_NAME)
        log_dir = data_dir / "logs" / "analytics"
    else:
        log_dir = basedir / "logs" / "analytics"

    log_dir.mkdir(parents=True, exist_ok=True)
    _prune_old_logs(log_dir)
    log_file = log_dir / f"analytics_{today_str}.json"

    # Ensure all required fields exist with default None if missing
    now_utc = datetime.now(timezone.utc)
    entry = {
        "timestamp": normalize_timestamp(now_utc.isoformat()),
        "table": 'analytics',
        'action': 'insert',
        'data': {
        "timestamp": normalize_timestamp(data.get("timestamp") or now_utc.isoformat()),
        "current_page": data.get("current_page"),
        "activity": data.get("activity"),
        "click": data.get("click"),
        "time_spent": data.get("time_spent"),
        "previous_page": data.get("previous_page"),
        "user": data.get("user"),
    }
    }

    try:
        if log_file.exists():
            with open(log_file, "r+", encoding="utf-8") as f:
                try:
                    existing_data = json.load(f)
                    if not isinstance(existing_data, list):
                        existing_data = []
                except json.JSONDecodeError:
                    existing_data = []
                existing_data.append(entry)
                f.seek(0)
                json.dump(existing_data, f, indent=2)
                f.truncate()
        else:
            with open(log_file, "w", encoding="utf-8") as f:
                json.dump([entry], f, indent=2)
    except Exception as e:
        print(f"Error logging user event: {e}")
