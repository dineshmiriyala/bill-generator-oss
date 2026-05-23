from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Sequence, Tuple, Optional

import shutil
import json
import requests

from db.db_events import get_sync_folder, obj_to_dict
from db.models import customer, invoice, invoiceItem, item, accountingTransaction

FAILED_DIR_NAME = "failed"
FAILED_EVENTS_FILE = "upload_events.json"
DATETIME_FIELDS = ("timestamp", "createdAt", "updatedAt", "deletedAt")
DEFAULT_CHUNK_SIZE = 200
DEFAULT_MAX_ATTEMPTS = 1
ACTIVITY_DIR_NAME = "activity"
ANALYTICS_DIR_NAME = "analytics"
ARCHIVE_DIR_NAME = "sent"
ARCHIVE_RETENTION_DAYS = 7
FAILED_RETENTION_DAYS = 7
FULL_SYNC_MODELS: Sequence[Tuple[Any, str]] = (
    (customer, customer.__tablename__),
    (item, item.__tablename__),
    (invoice, invoice.__tablename__),
    (invoiceItem, invoiceItem.__tablename__),
    (accountingTransaction, accountingTransaction.__tablename__),
)


@dataclass
class UploadResult:
    uploaded: int = 0
    failed: int = 0
    uploaded_records: List[Dict[str, Any]] = field(default_factory=list)
    failure_details: List[Dict[str, Any]] = field(default_factory=list)

    def record_success(self, table: str, action: str, record: Dict[str, Any]) -> None:
        self.uploaded += 1
        self.uploaded_records.append({"table": table, "action": action, "data": record})

    def record_failure(self, table: str, action: str, record: Dict[str, Any], details: Dict[str, Any]) -> None:
        self.failed += 1
        self.failure_details.append(
            {"table": table, "action": action, "record": record, "details": details}
        )

    def merge(self, other: "UploadResult") -> None:
        self.uploaded += other.uploaded
        self.failed += other.failed
        self.uploaded_records.extend(other.uploaded_records)
        self.failure_details.extend(other.failure_details)


class SupabaseUploadError(Exception):
    def __init__(
        self,
        message: str,
        failed_table: str,
        failed_count: int,
        skipped_tables: Sequence[str],
        table_results: Sequence[Tuple[str, UploadResult]],
        detail: Optional[str] = None,
    ) -> None:
        super().__init__(message if detail is None else f"{message} ({detail})")
        self.failed_table = failed_table
        self.failed_count = failed_count
        self.skipped_tables = list(skipped_tables)
        self.table_results = list(table_results)
        self.detail = detail


def _base_folder() -> Path:
    return Path(get_sync_folder())


def _failed_dir(base_folder: Path) -> Path:
    dir_path = base_folder / FAILED_DIR_NAME / datetime.now().strftime("%d-%m-%Y")
    dir_path.mkdir(parents=True, exist_ok=True)
    return dir_path


def _append_event(base_folder: Path, kind: str, payload: Dict[str, Any]) -> None:
    event = {"timestamp": datetime.now().isoformat(), "kind": kind}
    event.update(payload)
    log_file = _failed_dir(base_folder) / FAILED_EVENTS_FILE
    try:
        if log_file.exists():
            with open(log_file, "r", encoding="utf-8") as fh:
                events = json.load(fh)
        else:
            events = []
    except Exception:
        events = []

    events.append(event)
    try:
        with open(log_file, "w", encoding="utf-8") as fh:
            json.dump(events, fh, indent=2)
    except Exception:
        pass


def _build_headers(key: str) -> Dict[str, str]:
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates",
    }


def _endpoint(url: str, table: str) -> str:
    return f"{url}/rest/v1/{table}"


def _log_upload_activity(
    url: str,
    headers: Dict[str, str],
    base_folder: Path,
    result: UploadResult,
    **extra: Any,
) -> None:
    payload = {
        "user_id": "guest",
        "uploaded_at": datetime.now().isoformat(),
        "records_uploaded": result.uploaded,
        "failed": result.failed,
        "folder": str(base_folder),
        **extra,
    }
    try:
        tracking_endpoint = _endpoint(url, "upload_logs")
        resp = requests.post(tracking_endpoint, json=payload, headers=headers)
        _append_event(base_folder, "upload_logs_response", {"status_code": resp.status_code})
    except Exception as exc:  # pragma: no cover - best effort logging
        _append_event(base_folder, "upload_logs_exception", {"error": str(exc)})


def _normalize_ts(ts: Any) -> Any:
    if not isinstance(ts, str):
        return ts
    return ts.replace("T", " ").replace("Z", "+00")


def _normalize_record_datetimes(data: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(data, dict):
        return data
    normalized: Dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(value, dict):
            normalized[key] = _normalize_record_datetimes(value)
        elif isinstance(value, list):
            normalized[key] = [
                _normalize_record_datetimes(item) if isinstance(item, dict) else item
                for item in value
            ]
        elif key in DATETIME_FIELDS and isinstance(value, str):
            normalized[key] = _normalize_ts(value)
        else:
            normalized[key] = value
    return normalized


def _extract_error_details(response: requests.Response) -> Dict[str, Any]:
    try:
        parsed = response.json()
    except Exception:
        parsed = {"message": response.text}
    return {
        "status_code": response.status_code,
        "response": parsed,
    }


def _write_failed_record(
    base_folder: Path,
    table: str,
    action: str,
    record: Dict[str, Any],
    details: Dict[str, Any],
) -> None:
    failed_dir = _failed_dir(base_folder)
    timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S%f")[:-3]
    failed_file_path = failed_dir / f"failed_{table}_{timestamp_str}.json"
    failed_record = {
        "timestamp": datetime.now().isoformat(),
        "table": table,
        "action": action,
        "record": record,
        "details": details,
    }
    try:
        with open(failed_file_path, "w", encoding="utf-8") as fh:
            json.dump(failed_record, fh, indent=2)
    except Exception as exc:
        _append_event(base_folder, "failed_record_write_exception", {"error": str(exc)})
        return

    _append_event(
        base_folder,
        "record_upload_failed",
        {"file": str(failed_file_path), "table": table, "details": details},
    )


def _send_request(
    url: str,
    headers: Dict[str, str],
    table: str,
    action: str,
    record: Dict[str, Any],
) -> requests.Response:
    if action == "update":
        rec_id = record.get("id")
        if rec_id is None:
            raise ValueError(f"Update requires id for table {table}")
        url_q = f"{_endpoint(url, table)}?id=eq.{rec_id}"
        return requests.patch(url_q, json=record, headers=headers)
    if action == "delete":
        rec_id = record.get("id")
        if rec_id is None:
            raise ValueError(f"Delete requires id for table {table}")
        url_q = f"{_endpoint(url, table)}?id=eq.{rec_id}"
        return requests.delete(url_q, headers=headers)
    return requests.post(_endpoint(url, table), json=record, headers=headers)


def _process_records(
    url: str,
    headers: Dict[str, str],
    base_folder: Path,
    records: Iterable[Dict[str, Any]],
) -> UploadResult:
    result = UploadResult()
    for entry in records:
        table = entry.get("table")
        if not table:
            continue
        action = str(entry.get("action", "insert")).lower()
        record = _normalize_record_datetimes(entry.get("data", {}))
        try:
            response = _send_request(url, headers, table, action, record)
        except Exception as exc:
            result.record_failure(table, action, record, {"error": str(exc)})
            _append_event(base_folder, "upload_exception", {"error": str(exc), "table": table, "action": action})
            continue

        if response.status_code in (200, 201, 204):
            result.record_success(table, action, record)
        else:
            details = _extract_error_details(response)
            result.record_failure(table, action, record, details)
            _write_failed_record(base_folder, table, action, record, details)
    return result


def _load_logs_from_dir(directory: Path) -> Dict[Path, List[Dict[str, Any]]]:
    logs_by_file: Dict[Path, List[Dict[str, Any]]] = {}
    if not directory.exists():
        return logs_by_file
    for file in sorted(directory.glob("*.json")):
        try:
            with open(file, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception as exc:
            _append_event(directory.parent, "read_file_exception", {"file": str(file), "error": str(exc)})
            continue

        entries: List[Dict[str, Any]] = []
        if isinstance(data, list):
            for entry in data:
                if isinstance(entry, list):
                    entries.extend(entry)
                else:
                    entries.append(entry)
        elif isinstance(data, dict):
            entries.append(data)

        if entries:
            logs_by_file[file] = entries
    return logs_by_file


def _archive_processed_files(files: List[Path], archive_root: Path, subdir: str) -> int:
    if not files:
        return 0
    archive_dir = archive_root / subdir
    archive_dir.mkdir(parents=True, exist_ok=True)
    archived = 0
    for file in files:
        target = archive_dir / file.name
        try:
            if target.exists():
                target.unlink()
            shutil.move(str(file), str(target))
            archived += 1
        except Exception as exc:
            _append_event(archive_root.parent, "archive_move_failed", {"file": str(file), "error": str(exc)})
    return archived


def _cleanup_archive(archive_root: Path, retention_days: int = ARCHIVE_RETENTION_DAYS) -> None:
    if not archive_root.exists():
        return
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    for file in archive_root.rglob("*.json"):
        try:
            modified = datetime.fromtimestamp(file.stat().st_mtime, tz=timezone.utc)
            if modified < cutoff:
                file.unlink()
        except Exception as exc:
            _append_event(archive_root.parent, "archive_cleanup_failed", {"file": str(file), "error": str(exc)})


def _cleanup_failed_logs(base_folder: Path, retention_days: int = FAILED_RETENTION_DAYS) -> None:
    failed_root = base_folder / FAILED_DIR_NAME
    if not failed_root.exists():
        return
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    for path in failed_root.rglob("*.json"):
        try:
            modified = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            if modified < cutoff:
                path.unlink()
        except Exception as exc:
            _append_event(base_folder, "failed_cleanup_failed", {"file": str(path), "error": str(exc)})


def _chunk_records(records: List[Dict[str, Any]], chunk_size: int) -> Iterator[List[Dict[str, Any]]]:
    for index in range(0, len(records), chunk_size):
        yield records[index : index + chunk_size]


def _bulk_upsert_table(
    url: str,
    headers: Dict[str, str],
    base_folder: Path,
    table: str,
    records: List[Dict[str, Any]],
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> UploadResult:
    result = UploadResult()
    endpoint = _endpoint(url, table)

    for chunk in _chunk_records(records, chunk_size):
        attempt = 0
        while attempt < max_attempts:
            attempt += 1
            payload = [
                _normalize_record_datetimes(entry.get("data", {}))
                for entry in chunk
            ]
            try:
                response = requests.post(endpoint, json=payload, headers=headers)
            except Exception as exc:
                if attempt < max_attempts:
                    continue
                details = {"error": str(exc), "attempts": attempt}
                for entry in chunk:
                    action = entry.get("action", "insert")
                    data = _normalize_record_datetimes(entry.get("data", {}))
                    result.record_failure(table, action, data, details)
                    _append_event(
                        base_folder,
                        "upload_exception",
                        {"error": str(exc), "table": table, "action": action, "attempts": attempt},
                    )
                break

            if response.status_code in (200, 201, 204):
                for entry in chunk:
                    data = _normalize_record_datetimes(entry.get("data", {}))
                    result.record_success(table, entry.get("action", "insert"), data)
                break

            if attempt < max_attempts:
                continue

            details = _extract_error_details(response)
            details["attempts"] = attempt
            _append_event(
                base_folder,
                "bulk_upload_failed",
                {"table": table, "chunk_size": len(chunk), "details": details},
            )
            for entry in chunk:
                action = entry.get("action", "insert")
                data = _normalize_record_datetimes(entry.get("data", {}))
                result.record_failure(table, action, data, details)
                _write_failed_record(base_folder, table, action, data, details)
    return result


def _iter_activity_logs(base_folder: Path) -> List[Dict[str, Any]]:
    flattened_logs: List[Dict[str, Any]] = []
    for file in base_folder.rglob("*.json"):
        if FAILED_DIR_NAME in file.parts:
            continue
        try:
            with open(file, "r", encoding="utf-8") as fh:
                logs = json.load(fh)
        except Exception as exc:
            _append_event(base_folder, "read_file_exception", {"file": str(file), "error": str(exc)})
            continue

        if isinstance(logs, list):
            for log in logs:
                if isinstance(log, list):
                    flattened_logs.extend(log)
                else:
                    flattened_logs.append(log)
        elif isinstance(logs, dict):
            flattened_logs.append(logs)
    return flattened_logs


def upload_to_supabase(url: str, key: str, include_analytics: bool = True) -> Dict[str, Any]:
    base_folder = _base_folder()
    headers = _build_headers(key)
    activity_dir = base_folder / ACTIVITY_DIR_NAME
    analytics_dir = base_folder / ANALYTICS_DIR_NAME
    archive_root = base_folder / ARCHIVE_DIR_NAME

    activity_logs = _load_logs_from_dir(activity_dir)
    activity_entries: List[Dict[str, Any]] = [
        entry for entries in activity_logs.values() for entry in entries
    ]
    activity_result = (
        _process_records(url, headers, base_folder, activity_entries)
        if activity_entries
        else UploadResult()
    )
    activity_status = "DONE" if activity_result.failed == 0 else "FAILED"
    _log_upload_activity(url, headers, base_folder, activity_result, status=activity_status, mode="incremental-db")

    archived_activity = 0
    if activity_entries and activity_result.failed == 0:
        archived_activity = _archive_processed_files(
            list(activity_logs.keys()), archive_root, ACTIVITY_DIR_NAME
        )

    analytics_logs: Dict[Path, List[Dict[str, Any]]] = {}
    analytics_entries: List[Dict[str, Any]] = []
    analytics_result = UploadResult()
    archived_analytics = 0
    if include_analytics:
        analytics_logs = _load_logs_from_dir(analytics_dir)
        analytics_entries = [
            entry for entries in analytics_logs.values() for entry in entries
        ]
        analytics_result = (
            _process_records(url, headers, base_folder, analytics_entries)
            if analytics_entries
            else UploadResult()
        )
        analytics_status = "DONE" if analytics_result.failed == 0 else "FAILED"
        _log_upload_activity(
            url,
            headers,
            base_folder,
            analytics_result,
            status=analytics_status,
            mode="incremental-analytics",
        )

        if analytics_entries and analytics_result.failed == 0:
            archived_analytics = _archive_processed_files(
                list(analytics_logs.keys()), archive_root, ANALYTICS_DIR_NAME
            )

    summary_payload = {
        "total_uploaded": activity_result.uploaded + analytics_result.uploaded,
        "total_failed": activity_result.failed + analytics_result.failed,
        "status": "DONE" if activity_result.failed == analytics_result.failed == 0 else "PARTIAL",
        "db_uploaded": activity_result.uploaded,
        "db_failed": activity_result.failed,
        "analytics_uploaded": analytics_result.uploaded,
        "analytics_failed": analytics_result.failed,
    }
    _append_event(base_folder, "incremental_upload_summary", summary_payload)
    _cleanup_archive(archive_root)
    _cleanup_failed_logs(base_folder)

    return {
        "db": activity_result,
        "analytics": analytics_result,
        "archived": {
            "db": archived_activity,
            "analytics": archived_analytics,
        },
    }


def upload_full_database(url: str, key: str) -> UploadResult:
    base_folder = _base_folder()
    headers = _build_headers(key)
    cumulative = UploadResult()
    table_results: List[Tuple[str, UploadResult]] = []
    failure_payload: Optional[Dict[str, Any]] = None
    skipped_tables: List[str] = []

    for index, (model, table_name) in enumerate(FULL_SYNC_MODELS):
        entries = [
            {"table": table_name, "action": "insert", "data": obj_to_dict(instance)}
            for instance in model.query.all()
        ]
        if not entries:
            table_results.append((table_name, UploadResult()))
            continue

        table_result = _bulk_upsert_table(url, headers, base_folder, table_name, entries)
        table_results.append((table_name, table_result))
        cumulative.merge(table_result)

        if table_result.failed:
            detail_message: Optional[str] = None
            if table_result.failure_details:
                detail_payload = table_result.failure_details[0].get("details")
                if isinstance(detail_payload, dict):
                    detail_message = detail_payload.get("message") or detail_payload.get("error")
                elif detail_payload is not None:
                    detail_message = str(detail_payload)
            skipped_tables = [name for _, name in FULL_SYNC_MODELS[index + 1 :]]
            failure_payload = {
                "failed_table": table_name,
                "failed_count": table_result.failed,
                "skipped_tables": skipped_tables,
                "detail": detail_message,
            }
            break

    status = "FAILED" if failure_payload else "DONE"
    _log_upload_activity(url, headers, base_folder, cumulative, status=status, mode="full")
    summary_payload = {
        "total_uploaded": cumulative.uploaded,
        "total_failed": cumulative.failed,
        "status": status,
    }
    if failure_payload:
        summary_payload.update(failure_payload)
        if failure_payload.get("detail"):
            summary_payload["detail"] = failure_payload["detail"]
    _append_event(base_folder, "full_upload_summary", summary_payload)
    _cleanup_failed_logs(base_folder)

    if failure_payload:
        raise SupabaseUploadError(
            f"Supabase rejected {failure_payload['failed_count']} records in '{failure_payload['failed_table']}'.",
            failed_table=failure_payload["failed_table"],
            failed_count=failure_payload["failed_count"],
            skipped_tables=skipped_tables,
            table_results=table_results,
            detail=failure_payload.get("detail"),
        )

    return cumulative
