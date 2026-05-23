# --- 🔹 Automatic Sync Event Listeners for Cloud Staging ---
from sqlalchemy.orm import Session
from sqlalchemy import event
from datetime import datetime, date
from pathlib import Path
import os, json, sys

_activity_pending = False


def _mark_activity_pending() -> None:
    global _activity_pending
    _activity_pending = True


def activity_logs_pending() -> bool:
    return _activity_pending


def clear_activity_pending_flag() -> None:
    global _activity_pending
    _activity_pending = False

# Define tables to be tracked
SYNCED_TABLES = {"customer", "invoice", "item", "invoice_item", "accounting_transaction"}
# White-label: app name comes from env var so a downstream rebrand stays
# in sync with app.py without editing source.
APP_NAME = os.getenv("BG_APP_NAME", "Bill Generator")

# ---------------- PATH LOGIC ----------------
def _desktop_data_dir(app_name: str) -> Path:
    """Cross-platform base data directory for app storage."""
    if os.name == "nt":  # Windows
        return Path(os.getenv("APPDATA", str(Path.home() / "AppData" / "Roaming"))) / app_name
    elif sys.platform == "darwin":  # macOS
        return Path.home() / "Library" / "Application Support" / app_name
    else:  # Linux
        return Path.home() / ".local" / "share" / app_name

def get_sync_folder() -> Path:
    """Return correct sync staging folder path depending on environment."""
    is_desktop = os.getenv("BG_DESKTOP") == "1"
    if is_desktop:
        folder = _desktop_data_dir(APP_NAME) / "logs"
    else:
        folder = Path("logs")

    folder.mkdir(parents=True, exist_ok=True)
    return folder

# ---------------- LOGGING HELPERS ----------------
def stage_sync(table, action, data):
    """Append daily activity logs like analytics tracking."""
    if table not in SYNCED_TABLES:
        return  # skip non-core tables

    folder = get_sync_folder()  / "activity"
    folder.mkdir(parents=True, exist_ok=True)

    today_str = datetime.now().strftime("%Y_%m_%d")
    filename = f"activity_{today_str}.json"
    filepath = folder / filename

    entry = {
        "timestamp": datetime.now().isoformat(),
        "table": table,
        "action": action,
        "data": data
    }

    try:
        if filepath.exists():
            with open(filepath, "r", encoding="utf-8") as f:
                try:
                    existing = json.load(f)
                    if not isinstance(existing, list):
                        existing = []
                except json.JSONDecodeError:
                    existing = []
        else:
            existing = []

        existing.append(entry)

        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(existing, f, indent=2, ensure_ascii=False)

        print(f"[append OK] Logged {table} {action} in {filename}")
        _mark_activity_pending()
    except Exception as e:
        print(f"[append WARN] Failed to log {table} {action}: {e}")

def obj_to_dict(obj):
    """Convert SQLAlchemy ORM object to JSON-safe dict."""
    result = {}
    for col in obj.__table__.columns:
        val = getattr(obj, col.name, None)
        if isinstance(val, (datetime, date)):
            result[col.name] = val.isoformat()  # serialize datetime/date
        else:
            result[col.name] = val
    return result

# ---------------- EVENT LISTENERS ----------------
@event.listens_for(Session, "after_flush")
def track_local_db_changes(session, flush_context):
    """Track inserts, updates, deletes automatically."""
    for obj in list(session.new):
        table = getattr(obj.__table__, "name", None)
        if table in SYNCED_TABLES:
            stage_sync(table, "insert", obj_to_dict(obj))

    for obj in list(session.dirty):
        table = getattr(obj.__table__, "name", None)
        if table in SYNCED_TABLES and session.is_modified(obj, include_collections=False):
            stage_sync(table, "update", obj_to_dict(obj))

    for obj in list(session.deleted):
        table = getattr(obj.__table__, "name", None)
        if table in SYNCED_TABLES:
            stage_sync(table, "delete", obj_to_dict(obj))

# ---------------- NEW: AFTER COMMIT LISTENER ----------------
@event.listens_for(Session, "after_commit")
def track_after_commit(session):
    """Ensure all related or dependent inserts (like invoiceItems) are logged post-commit."""
    try:
        all_objects = getattr(session, "new", [])
        for obj in list(all_objects):
            table = getattr(obj.__table__, "name", None)
            if table in SYNCED_TABLES:
                stage_sync(table, "post_commit_insert", obj_to_dict(obj))
    except Exception as e:
        print(f"[after_commit WARN] Error tracking dependent inserts: {e}")

@event.listens_for(Session, "after_commit")
def confirm_commit(session):
    """Optional: print commit summary."""
    if any([session.new, session.dirty, session.deleted]):
        print(f"[commit OK] {len(session.new)} inserted, {len(session.dirty)} updated, {len(session.deleted)} deleted.")
