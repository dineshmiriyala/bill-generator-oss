import atexit
import csv
import io
import json
import logging
import os
import shutil
import sys
import sqlite3
import uuid
import re
from collections import defaultdict
from copy import deepcopy
from types import SimpleNamespace
from typing import Dict, List, Optional
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from pathlib import Path
from urllib.parse import urlparse
import socket
from waitress import serve

import requests
import stat
from dateutil import tz, parser
from flask import (
    Flask,
    Response,
    flash,
    jsonify,
    redirect,
    render_template,
    render_template_string,
    request,
    send_file,
    session,
    url_for,
)
from flask_migrate import Migrate
from sqlalchemy import func, inspect, or_
from sqlalchemy.orm import joinedload

from analytics import (
    get_customer_retention,
    get_day_wise_billing,
    get_sales_trends,
    get_top_customers,
)
from analytics_tracking import log_user_event
from api import api_bp
from db.db_events import activity_logs_pending, clear_activity_pending_flag  # noqa: F401
from db.models import db, customer, invoice, invoiceItem, item, layoutConfig, accountingTransaction, expenseItem, billDraft
from migration import migrate_db
from supabase_upload import SupabaseUploadError, upload_full_database, upload_to_supabase

# --- Branding / white-label settings ----------------------------------------
# These can be overridden via environment variables so a downstream
# distribution (or end-user packaging) can rebrand the app without
# editing source files. Defaults are kept generic for the open-source
# release.
APP_NAME = os.getenv("BG_APP_NAME", "Bill Generator")
APP_VERSION = os.getenv("BG_APP_VERSION", "4.5.1")
DEFAULT_INVOICE_PREFIX = os.getenv("BG_INVOICE_PREFIX", "INV")
DEFAULT_TXN_PREFIX = os.getenv("BG_TXN_PREFIX", "TXN")
# ----------------------------------------------------------------------------

BG_DESKTOP_ENV = "BG_DESKTOP"
# SECRET_KEY is generated per-install and persisted in DATA_DIR (see below).
# The literal default is only used as a last-resort fallback for tests; in
# production the app generates and reuses a 32-byte random key on first run.
DEFAULT_SECRET_KEY = "dev-only-secret-do-not-use-in-production"
SECRET_KEY_FILENAME = "secret.key"
DATABASE_FILENAME = "app.db"
INFO_FILENAME = "info.json"
DEFAULT_TIMEZONE = "Asia/Kolkata"
REQUIRED_DB_TABLES = {"customer", "invoice", "invoice_item", "item"}
BACKUP_DIRNAME = "backups"
BACKUP_RETENTION = 10
BACKUP_MAX_AGE_DAYS = 7
APP_PORT = 42069
ISO_8601_UTC = "%Y-%m-%dT%H:%M:%SZ"
HUMAN_DATE_FMT = "%d %B %Y"
DEFAULT_LOGO_COLOR_MODE = "black"
LOGO_COLOR_PATHS = {
    "black": "static/img/brand-water-mark-black.svg",
    "blue": "static/img/brand-water-mark-blue.svg",
}
LOGO_COLOR_VALUES = {
    "black": "#111111",
    "blue": "#1b4ea0",
}
DEFAULT_TO_COLOR_MODE = "black"
TO_COLOR_VALUES = {
    "black": "#000000",
    "magenta": "#FF00FF",
}
TO_COLOR_MODES = set(TO_COLOR_VALUES.keys()) | {"custom"}
HEX_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{6})$")
ACCOUNTING_STATEMENT_DEFAULT_START = datetime(2025, 9, 1).date()


def _default_info_sections(reference_dt: Optional[datetime] = None) -> dict:
    """Return a fresh copy of default info.json sections."""
    reference_dt = reference_dt or datetime.now(timezone.utc)
    iso_now = reference_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    current_year = str(reference_dt.year)

    return {
        "business": {
            "name": "",
            "tagline": "",
            "owner": "",
            "address": "",
            "email": "",
            "phone": "",
            "gstin": "",
            "upi_id": "",
            "upi_name": "",
            "businessType": "",
            "pan": "",
            "estd": current_year,
            "logo_path": LOGO_COLOR_PATHS[DEFAULT_LOGO_COLOR_MODE],
            "logo_color_mode": DEFAULT_LOGO_COLOR_MODE,
        },
        "bank": {
            "account_name": "",
            "bank_name": "",
            "branch": "",
            "account_number": "",
            "ifsc": "",
            "bhim": "",
        },
        "appearance": {
            "currency_symbol": "\\u20b9",
            "date_format": "%d %B %Y",
            "font_family": "Inter, Helvetica, Arial, sans-serif",
            "theme_color": "#0056b3",
        },
        "invoice_visual": {
            "to_color_mode": DEFAULT_TO_COLOR_MODE,
            "to_color_custom": "",
        },
        "payment": {
            "methods": ["Bank Transfer", "UPI"],
            "upi_qr_enabled": False,
            "qr_label": "Scan to Pay",
            "terms": [
                "Payment due within 7 days of invoice date.",
                "Late payments may incur a 2% interest per month.",
                "This is a computer-generated document, no signature required.",
            ],
        },
        "statement": {
            "header_title": "Statement Summary",
            "disclaimer": "This is a system-generated statement. No signature required.",
        },
        "account_defaults": {
            "start_date": iso_now,
            "timezone": DEFAULT_TIMEZONE,
        },
        "meta": {
            "version": APP_VERSION,
            "created_on": iso_now,
        },
        "upi_info": {
            "upi_id": "",
            "currency": "INR",
            "upi_name": "",
        },
        "services": [
            "Printing",
            "Design",
            "Branding Collateral",
        ],
        "bill_config": {
            "heading": "Tax Invoice",
            "footer": "Composition Taxable Person. Not eligible to collect Tax on supplies.",
            "payment-footer": "Computer generated receipt - Signature not required",
            "dues-table-position": "below_totals",
            "dues-table-heading": "All Past Dues",
            # Prefix used when generating invoice IDs (e.g. "INV-280526-00042").
            # Override per-install by editing this value or setting BG_INVOICE_PREFIX.
            "invoice_prefix": DEFAULT_INVOICE_PREFIX,
        },
        "file_location": "",
        "supabase": {
            "url": "",
            "key": "",
            "last_uploaded": "",
            "last_incremental_uploaded": "",
            "instant_uploads": False,
        },
    }


def _merge_missing(target: dict, defaults: dict) -> bool:
    """Merge missing keys from defaults into target. Returns True if mutated."""
    changed = False
    for key, default_value in defaults.items():
        if key not in target:
            target[key] = deepcopy(default_value)
            changed = True
        else:
            current_value = target[key]
            if isinstance(default_value, dict) and isinstance(current_value, dict):
                if _merge_missing(current_value, default_value):
                    changed = True
    return changed


def _ensure_utc(dt: datetime) -> datetime:
    """Normalize datetimes to UTC with timezone info."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _format_iso_utc(dt: datetime) -> str:
    return _ensure_utc(dt).strftime(ISO_8601_UTC)


def _format_human_date(dt: datetime) -> str:
    return _ensure_utc(dt).strftime(HUMAN_DATE_FMT)


def _resolve_brand_watermark_path(business_section: Optional[dict]) -> str:
    """Return the static asset path for the selected brand watermark color."""
    if not isinstance(business_section, dict):
        business_section = {}
    color_mode = (business_section.get("logo_color_mode") or DEFAULT_LOGO_COLOR_MODE).lower()
    return LOGO_COLOR_PATHS.get(color_mode, LOGO_COLOR_PATHS[DEFAULT_LOGO_COLOR_MODE])


def _resolve_brand_accent_color(business_section: Optional[dict]) -> str:
    """Return the hex color used for accent text."""
    if not isinstance(business_section, dict):
        business_section = {}
    color_mode = (business_section.get("logo_color_mode") or DEFAULT_LOGO_COLOR_MODE).lower()
    return LOGO_COLOR_VALUES.get(color_mode, LOGO_COLOR_VALUES[DEFAULT_LOGO_COLOR_MODE])


def _normalize_logo_color_mode(value: Optional[str]) -> str:
    color_mode = (value or DEFAULT_LOGO_COLOR_MODE).strip().lower()
    if color_mode not in LOGO_COLOR_PATHS:
        return DEFAULT_LOGO_COLOR_MODE
    return color_mode


def _normalize_to_color_mode(value: Optional[str]) -> str:
    color_mode = (value or DEFAULT_TO_COLOR_MODE).strip().lower()
    if color_mode not in TO_COLOR_MODES:
        return DEFAULT_TO_COLOR_MODE
    return color_mode


def _normalize_hex_color(value: Optional[str]) -> Optional[str]:
    raw = (value or "").strip()
    if not raw:
        return None
    if HEX_COLOR_RE.match(raw):
        return raw.upper()
    return None


def _resolve_to_color(visual_section: Optional[dict]) -> str:
    if not isinstance(visual_section, dict):
        visual_section = {}
    color_mode = _normalize_to_color_mode(visual_section.get("to_color_mode"))
    if color_mode == "custom":
        custom = _normalize_hex_color(visual_section.get("to_color_custom"))
        if custom:
            return custom
        return TO_COLOR_VALUES[DEFAULT_TO_COLOR_MODE]
    return TO_COLOR_VALUES.get(color_mode, TO_COLOR_VALUES[DEFAULT_TO_COLOR_MODE])


def _sync_logo_color_settings(business_section: dict) -> bool:
    if not isinstance(business_section, dict):
        return False
    changed = False
    color_mode = _normalize_logo_color_mode(business_section.get("logo_color_mode"))
    if business_section.get("logo_color_mode") != color_mode:
        business_section["logo_color_mode"] = color_mode
        changed = True

    logo_path = business_section.get("logo_path")
    default_logo_paths = set(LOGO_COLOR_PATHS.values())
    default_logo_paths.add("static/img/brand-wordmark.svg")
    if not logo_path or logo_path in default_logo_paths:
        desired_path = LOGO_COLOR_PATHS[color_mode]
        if logo_path != desired_path:
            business_section["logo_path"] = desired_path
            changed = True
    return changed


def _sync_to_color_settings(visual_section: dict) -> bool:
    if not isinstance(visual_section, dict):
        return False
    changed = False
    color_mode = _normalize_to_color_mode(visual_section.get("to_color_mode"))
    if visual_section.get("to_color_mode") != color_mode:
        visual_section["to_color_mode"] = color_mode
        changed = True
    if "to_color_custom" in visual_section:
        normalized_custom = _normalize_hex_color(visual_section.get("to_color_custom"))
        if normalized_custom:
            if visual_section.get("to_color_custom") != normalized_custom:
                visual_section["to_color_custom"] = normalized_custom
                changed = True
        else:
            if visual_section.get("to_color_custom"):
                visual_section["to_color_custom"] = ""
                changed = True
    return changed


def _sanitize_filename_component(value: Optional[str], fallback: str = "statement") -> str:
    """Return a filesystem-friendly slug (A-Z, 0-9, underscores) for titles."""
    value = (value or fallback).strip()
    safe = re.sub(r"[^A-Za-z0-9]+", "_", value)
    safe = re.sub(r"_+", "_", safe).strip("_")
    return safe or fallback


def _build_statement_pdf_title(company_name: Optional[str], start_date, end_date) -> str:
    """Compose a predictable print/PDF title with company + date range."""
    start_str = start_date.strftime("%Y-%m-%d") if start_date else "start"
    end_str = end_date.strftime("%Y-%m-%d") if end_date else "end"
    return f"{_sanitize_filename_component(company_name)}_{start_str}_{end_str}_statement"


def _build_export_pdf_title(label: Optional[str], kind: str = "statement", generated_at: Optional[datetime] = None) -> str:
    """Compose a predictable print/PDF title with a label and today's date."""
    generated_at = generated_at or datetime.now(timezone.utc)
    date_str = generated_at.astimezone(timezone.utc).strftime("%Y-%m-%d")
    return f"{_sanitize_filename_component(label)}_{date_str}_{_sanitize_filename_component(kind, 'statement')}"


def _get_earliest_invoice_created_at() -> Optional[datetime]:
    """Fetch the earliest invoice.createdAt value from the DB."""
    try:
        with app.app_context():
            earliest = (
                db.session.query(func.min(invoice.createdAt))
                .filter(invoice.isDeleted == False)  # noqa: E712
                .scalar()
            )
    except Exception as exc:
        print(f"[warn] Failed to determine earliest invoice date: {exc}")
        return None

    if not earliest:
        return None

    if isinstance(earliest, str):
        try:
            earliest = datetime.strptime(earliest, ISO_8601_UTC)
        except (ValueError, TypeError) as exc:
            logger.warning("Could not parse earliest invoice createdAt %r: %s", earliest, exc)
            return None

    return _ensure_utc(earliest)


def _determine_data_start(now: datetime) -> datetime:
    """Return the correct account start date, preferring the first invoice date."""
    earliest = _get_earliest_invoice_created_at()
    if not earliest:
        return now

    earliest_utc = _ensure_utc(earliest)
    # Guard against corrupted future dates
    if earliest_utc > now:
        return now
    return earliest_utc


def _issue_bill_token() -> str:
    token = uuid.uuid4().hex
    session['bill_form_token'] = token
    return token


def _validate_bill_token(submitted: str) -> bool:
    expected = session.get('bill_form_token')
    if not expected or not submitted or submitted != expected:
        return False
    session.pop('bill_form_token', None)
    return True


logger = logging.getLogger(__name__)


def _safe_commit(action_label: str) -> bool:
    """Commit the current session; rollback and log on failure. Returns True on success."""
    try:
        db.session.commit()
        return True
    except Exception:
        db.session.rollback()
        logger.exception("DB commit failed during %s", action_label)
        return False


def _get_customer_bill_history(
    customer_id: Optional[int],
    exclude_invoice_id: Optional[str] = None,
    *,
    start_dt: Optional[datetime] = None,
    end_dt: Optional[datetime] = None,
) -> List[Dict[str, object]]:
    """Return non-deleted invoice summaries for the create-bill history panel."""
    if not customer_id:
        return []

    history_query = (
        db.session.query(
            invoice.invoiceId.label('invoice_no'),
            invoice.createdAt.label('created_at'),
            invoice.totalAmount.label('total_amount'),
            invoice.payment.label('is_paid'),
            func.count(invoiceItem.id).label('item_count'),
        )
        .outerjoin(invoiceItem, invoiceItem.invoiceId == invoice.id)
        .filter(
            invoice.customerId == customer_id,
            invoice.isDeleted.is_(False),
        )
    )

    if exclude_invoice_id:
        history_query = history_query.filter(invoice.invoiceId != exclude_invoice_id)
    if start_dt:
        history_query = history_query.filter(invoice.createdAt >= start_dt)
    if end_dt:
        history_query = history_query.filter(invoice.createdAt <= end_dt)

    rows = (
        history_query
        .group_by(invoice.id)
        .order_by(invoice.createdAt.desc(), invoice.id.desc())
        .all()
    )

    return [
        {
            'invoice_no': row.invoice_no,
            'created_at': row.created_at,
            'total_amount': float(row.total_amount or 0.0),
            'is_paid': bool(row.is_paid),
            'item_count': int(row.item_count or 0),
        }
        for row in rows
    ]


def _get_customer_transaction_invoice_rows(customer_id: Optional[int]) -> List[Dict[str, object]]:
    if not customer_id:
        return []

    payments_subq = _invoice_income_payments_subquery()
    history_rows = (
        db.session.query(
            invoice.invoiceId.label('invoice_no'),
            invoice.createdAt.label('created_at'),
            invoice.totalAmount.label('total_amount'),
            func.coalesce(payments_subq.c.paid_amount, 0.0).label('paid_amount'),
        )
        .outerjoin(payments_subq, payments_subq.c.invoice_no == invoice.invoiceId)
        .filter(
            invoice.customerId == customer_id,
            invoice.isDeleted.is_(False),
        )
        .order_by(invoice.createdAt.desc(), invoice.id.desc())
        .all()
    )

    rows = []
    for row in history_rows:
        outstanding_amount = round(max(float(row.total_amount or 0.0) - float(row.paid_amount or 0.0), 0.0), 2)
        rows.append({
            'invoice_no': row.invoice_no,
            'created_at': row.created_at,
            'date_label': row.created_at.strftime('%d %b %Y') if row.created_at else '',
            'total_amount': float(round(row.total_amount or 0.0, 2)),
            'outstanding_amount': float(round(outstanding_amount or 0.0, 2)),
            'is_paid': bool(outstanding_amount <= 0.01),
            'selectable': bool(outstanding_amount > 0.01),
        })
    return rows


def _safe_local_redirect(raw_target: Optional[str], fallback: str) -> str:
    target = (raw_target or '').strip()
    if not target:
        return fallback
    parsed = urlparse(target)
    if parsed.netloc and parsed.netloc != request.host:
        return fallback
    if not parsed.path:
        return fallback
    return target


def _redirect_missing_customer(
    fallback: str,
    *,
    raw_target: Optional[str] = None,
    message: str = 'This customer does not exist anymore.',
):
    flash(message, 'warning')
    target = _safe_local_redirect(raw_target or request.referrer, fallback)
    current_candidates = {
        request.path,
        request.full_path.rstrip('?'),
        request.url,
    }
    if target in current_candidates:
        target = fallback
    return redirect(target)


def _post_create_customer_redirect(customer_obj) -> str:
    requested_next = _safe_local_redirect(request.form.get('next_url'), '')
    wants_bill_flow = _draft_flag_enabled(request.form.get('bill_generation'))

    if wants_bill_flow or urlparse(requested_next).path == url_for('start_bill'):
        return url_for('start_bill', customer_id=customer_obj.id)
    if requested_next:
        return requested_next
    return url_for('about_user', customer_id=customer_obj.id)


def _draft_flag_enabled(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or '').strip().lower() in {'1', 'true', 'yes', 'on'}


def _clean_form_text(value) -> str:
    return (value or '').strip()


def _format_form_number(value, *, places: Optional[int] = None) -> str:
    if value in (None, ''):
        return ''
    try:
        numeric = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return str(value)
    if places is not None:
        quantizer = Decimal('1') if places == 0 else Decimal(f"1.{'0' * places}")
        numeric = numeric.quantize(quantizer, rounding=ROUND_HALF_UP)
        return f"{numeric:.{places}f}"
    normalized = format(numeric.normalize(), 'f')
    if '.' in normalized:
        normalized = normalized.rstrip('0').rstrip('.')
    return normalized or '0'


def _compute_bill_row_total(quantity_raw, rate_raw, rounded) -> Optional[Decimal]:
    quantity_text = _clean_form_text(quantity_raw)
    rate_text = _clean_form_text(rate_raw)
    if not quantity_text or not rate_text:
        return None
    try:
        quantity_value = Decimal(quantity_text)
        rate_value = Decimal(rate_text)
    except (InvalidOperation, TypeError, ValueError):
        return None

    raw_total = quantity_value * rate_value
    if rounded:
        rounded_total = Decimal(str(rounding_to_nearest_zero(raw_total)))
        return rounded_total.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    return raw_total.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def _load_bill_draft_payload(raw_payload: Optional[str]) -> Dict[str, object]:
    if not raw_payload:
        return {}
    try:
        payload = json.loads(raw_payload)
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _serialize_bill_draft_payload(form) -> Dict[str, object]:
    descriptions = form.getlist('description[]')
    quantities = form.getlist('quantity[]')
    rates = form.getlist('rate[]')
    dc_numbers = form.getlist('dc_no[]')
    rounded_flags = form.getlist('rounded[]')
    submitted_totals = form.getlist('total[]')

    row_count = max(
        len(descriptions),
        len(quantities),
        len(rates),
        len(dc_numbers),
        len(rounded_flags),
        len(submitted_totals),
        1,
    )

    items_payload = []
    total_amount = Decimal('0.00')

    for index in range(row_count):
        description = _clean_form_text(descriptions[index] if index < len(descriptions) else '')
        quantity = _clean_form_text(quantities[index] if index < len(quantities) else '')
        rate = _clean_form_text(rates[index] if index < len(rates) else '')
        dc_no = _clean_form_text(dc_numbers[index] if index < len(dc_numbers) else '')
        rounded = index < len(rounded_flags) and rounded_flags[index] == '1'
        submitted_total = _clean_form_text(submitted_totals[index] if index < len(submitted_totals) else '')

        if not any([description, quantity, rate, dc_no, submitted_total, rounded]):
            continue

        items_payload.append({
            'description': description,
            'quantity': quantity,
            'rate': rate,
            'dc_no': dc_no,
            'rounded': bool(rounded),
        })

        line_total = _compute_bill_row_total(quantity, rate, rounded)
        if line_total is not None:
            total_amount += line_total

    payload = {
        'exclude_phone': _draft_flag_enabled(form.get('exclude_phone')),
        'exclude_gst': _draft_flag_enabled(form.get('exclude_gst')),
        'exclude_addr': _draft_flag_enabled(form.get('exclude_addr')),
        'dc_enabled': _draft_flag_enabled(form.get('dc_enabled')),
        'items': items_payload,
    }

    return {
        'payload': payload,
        'payload_json': json.dumps(payload),
        'item_count': len(items_payload),
        'total_amount': float(total_amount.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)),
    }


def _build_bill_draft_form_context(draft_record: billDraft) -> Dict[str, object]:
    payload = _load_bill_draft_payload(draft_record.payloadJson)
    items_payload = payload.get('items') or []

    descriptions = []
    quantities = []
    rates = []
    dc_numbers = []
    rounded_flags = []
    line_totals = []
    total_amount = Decimal('0.00')

    for row in items_payload:
        if not isinstance(row, dict):
            continue
        description = _clean_form_text(row.get('description'))
        quantity = _clean_form_text(row.get('quantity'))
        rate = _clean_form_text(row.get('rate'))
        dc_no = _clean_form_text(row.get('dc_no'))
        rounded = _draft_flag_enabled(row.get('rounded'))
        line_total = _compute_bill_row_total(quantity, rate, rounded)

        descriptions.append(description)
        quantities.append(quantity)
        rates.append(rate)
        dc_numbers.append(dc_no)
        rounded_flags.append('1' if rounded else '0')
        line_totals.append(_format_form_number(line_total, places=2) if line_total is not None else '')

        if line_total is not None:
            total_amount += line_total

    total_amount = total_amount.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

    return {
        'descriptions': descriptions,
        'quantities': quantities,
        'rates': rates,
        'dc_numbers': dc_numbers,
        'rounded_flags': rounded_flags,
        'line_totals': line_totals,
        'dcno': _draft_flag_enabled(payload.get('dc_enabled')),
        'exclude_phone': _draft_flag_enabled(payload.get('exclude_phone')),
        'exclude_gst': _draft_flag_enabled(payload.get('exclude_gst')),
        'exclude_addr': _draft_flag_enabled(payload.get('exclude_addr')),
        'total': float(total_amount),
        'grand_total': float(total_amount),
        'show_prefilled_rows': bool(descriptions),
    }


def _get_active_draft_counts(customer_ids: Optional[List[int]] = None) -> Dict[int, int]:
    counts_query = (
        db.session.query(
            billDraft.customerId,
            func.count(billDraft.id),
        )
        .join(customer, customer.id == billDraft.customerId)
        .filter(
            billDraft.status == 'draft',
            customer.isDeleted.is_(False),
        )
    )
    if customer_ids is not None:
        if not customer_ids:
            return {}
        counts_query = counts_query.filter(billDraft.customerId.in_(customer_ids))

    rows = counts_query.group_by(billDraft.customerId).all()
    return {int(customer_id): int(count or 0) for customer_id, count in rows}


def _build_bill_draft_payload_from_invoice(invoice_obj: invoice) -> Dict[str, object]:
    items_payload = []
    line_items = (
        invoiceItem.query
        .filter_by(invoiceId=invoice_obj.id)
        .order_by(invoiceItem.id.asc())
        .all()
    )
    dc_enabled = False
    for line_item in line_items:
        inventory_item = db.session.get(item, line_item.itemId)
        dc_no = _clean_form_text(line_item.dcNo)
        if dc_no:
            dc_enabled = True
        items_payload.append({
            'description': inventory_item.name if inventory_item else 'Unknown',
            'quantity': _format_form_number(line_item.quantity),
            'rate': _format_form_number(line_item.rate, places=2),
            'dc_no': dc_no,
            'rounded': bool(getattr(line_item, 'rounded', False)),
        })

    payload = {
        'exclude_phone': bool(getattr(invoice_obj, 'exclude_phone', False)),
        'exclude_gst': bool(getattr(invoice_obj, 'exclude_gst', False)),
        'exclude_addr': bool(getattr(invoice_obj, 'exclude_addr', False)),
        'dc_enabled': dc_enabled,
        'items': items_payload,
    }

    return {
        'payload': payload,
        'payload_json': json.dumps(payload),
        'item_count': len(items_payload),
        'total_amount': float(Decimal(str(invoice_obj.totalAmount or 0)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)),
    }


def _resolve_accounting_customer_search(raw_query: str):
    query = (raw_query or '').strip()
    if not query:
        return None

    normalized = query.lower()
    alive_query = customer.alive()

    exact_phone = (
        alive_query
        .filter(func.lower(func.coalesce(customer.phone, '')) == normalized)
        .order_by(customer.name.asc(), customer.id.asc())
        .first()
    )
    if exact_phone:
        return exact_phone

    try:
        customer_id = int(query)
    except (TypeError, ValueError):
        customer_id = None
    if customer_id:
        exact_id = alive_query.filter(customer.id == customer_id).first()
        if exact_id:
            return exact_id

    exact_company = (
        alive_query
        .filter(func.lower(func.coalesce(customer.company, '')) == normalized)
        .order_by(customer.name.asc(), customer.id.asc())
        .first()
    )
    if exact_company:
        return exact_company

    exact_name = (
        alive_query
        .filter(func.lower(func.coalesce(customer.name, '')) == normalized)
        .order_by(customer.name.asc(), customer.id.asc())
        .first()
    )
    if exact_name:
        return exact_name

    like_value = f"%{normalized}%"
    matches = (
        alive_query
        .filter(
            or_(
                func.lower(func.coalesce(customer.name, '')).like(like_value),
                func.lower(func.coalesce(customer.company, '')).like(like_value),
                func.lower(func.coalesce(customer.phone, '')).like(like_value),
            )
        )
        .all()
    )
    if not matches:
        return None

    def _rank(cust):
        phone_value = (cust.phone or '').lower()
        company_value = (cust.company or '').lower()
        name_value = (cust.name or '').lower()
        return (
            0 if phone_value.startswith(normalized) else 1,
            0 if company_value.startswith(normalized) else 1,
            0 if name_value.startswith(normalized) else 1,
            company_value or name_value or phone_value,
            cust.id,
        )

    matches.sort(key=_rank)
    return matches[0]


def _find_customer_by_exact_phone(raw_phone: str):
    normalized = (raw_phone or '').strip().lower()
    if not normalized:
        return None
    return (
        customer.alive()
        .filter(func.lower(func.coalesce(customer.phone, '')) == normalized)
        .order_by(customer.id.asc())
        .first()
    )


def _resolve_statement_customer_token(raw_token: str):
    token = (raw_token or '').strip()
    if not token:
        return None

    if token.isdigit():
        exact_id_match = customer.alive().filter_by(id=int(token)).first()
        if exact_id_match:
            return exact_id_match

    return _find_customer_by_exact_phone(token)


def _get_customer_activity_date_bounds(customer_id: int) -> Dict[str, object]:
    today = datetime.now(timezone.utc).date()

    invoice_min, invoice_max = (
        db.session.query(func.min(invoice.createdAt), func.max(invoice.createdAt))
        .filter(
            invoice.customerId == customer_id,
            invoice.isDeleted.is_(False),
        )
        .one()
    )
    txn_min, txn_max = (
        db.session.query(
            func.min(accountingTransaction.created_at),
            func.max(accountingTransaction.created_at)
        )
        .filter(
            accountingTransaction.customerId == customer_id,
            accountingTransaction.is_deleted.is_(False),
        )
        .one()
    )

    all_dates = [value.date() for value in (invoice_min, invoice_max, txn_min, txn_max) if value]
    if not all_dates:
        return {
            'start_date': today,
            'end_date': today,
        }

    return {
        'start_date': min(all_dates),
        'end_date': max(max(all_dates), today),
    }


def _build_accounting_modal_context(*, preset_customer_id: Optional[int] = None, next_url: Optional[str] = None) -> Dict[str, object]:
    customers_list = customer.alive().order_by(customer.name.asc()).all()
    return {
        'customers': customers_list,
        'payment_modes': ['cash', 'bank', 'upi'],
        'account_options': ['cash', 'savings', 'current'],
        'business_expense_id': _ensure_business_expense_customer().id,
        'preset_customer_id': preset_customer_id,
        'modal_next_url': next_url,
    }


def _build_accounting_customer_page_context(
    customer_obj,
    *,
    start_date,
    end_date,
    filters_active: bool,
) -> Dict[str, object]:
    start_dt = datetime.combine(start_date, datetime.min.time()).replace(tzinfo=timezone.utc)
    end_dt = datetime.combine(end_date, datetime.max.time()).replace(tzinfo=timezone.utc)

    invoice_history = _get_customer_bill_history(
        customer_obj.id,
        start_dt=start_dt,
        end_dt=end_dt,
    )
    transactions = (
        accountingTransaction.query
        .options(joinedload(accountingTransaction.customer), joinedload(accountingTransaction.expense_items))
        .filter(
            accountingTransaction.customerId == customer_obj.id,
            accountingTransaction.is_deleted.is_(False),
            accountingTransaction.created_at >= start_dt,
            accountingTransaction.created_at <= end_dt,
        )
        .order_by(accountingTransaction.created_at.desc(), accountingTransaction.id.desc())
        .all()
    )

    total_invoiced = round(sum(entry['total_amount'] for entry in invoice_history), 2)
    total_paid = round(
        sum(float(txn.amount or 0.0) for txn in transactions if txn.txn_type == 'income'),
        2,
    )
    total_expenses = round(
        sum(float(txn.amount or 0.0) for txn in transactions if txn.txn_type == 'expense'),
        2,
    )
    raw_balance = round(total_invoiced + total_expenses - total_paid, 2)
    balance_due = round(max(raw_balance, 0.0), 2)
    credit_amount = round(max(total_paid - (total_invoiced + total_expenses), 0.0), 2)

    bounds = _get_customer_activity_date_bounds(customer_obj.id)
    range_label = (
        f"{start_date.strftime('%d %b %Y')} — {end_date.strftime('%d %b %Y')}"
        if filters_active else
        'All time'
    )

    return {
        'accounting_customer': customer_obj,
        'invoice_history': invoice_history,
        'transactions': transactions,
        'summary': {
            'total_invoiced': total_invoiced,
            'total_paid': total_paid,
            'total_expenses': total_expenses,
            'balance_due': balance_due,
            'credit_amount': credit_amount,
            'invoice_count': len(invoice_history),
            'transaction_count': len(transactions),
        },
        'filters': {
            'start': start_date.isoformat(),
            'end': end_date.isoformat(),
            'active': filters_active,
            'label': range_label,
            'all_time_start': bounds['start_date'].isoformat(),
            'all_time_end': bounds['end_date'].isoformat(),
        },
        'print_url': url_for(
            'accounting_customer_statement',
            customer_id=customer_obj.id,
            start=start_date.isoformat(),
            end=end_date.isoformat(),
        ),
        'simple_print_url': url_for(
            'accounting_customer_simple_statement',
            customer_id=customer_obj.id,
            start=start_date.isoformat(),
            end=end_date.isoformat(),
        ),
    }


def _resolve_legacy_statement_dates() -> tuple:
    scope = (request.args.get('scope') or 'custom').lower()
    today = datetime.now(timezone.utc).date()
    min_allowed_start = get_default_statement_start().date()

    start_date = None
    end_date = None

    if scope == 'year':
        try:
            year = int(request.args.get('year') or today.year)
            start_date = datetime(year, 1, 1).date()
            end_date = datetime(year, 12, 31).date()
        except (TypeError, ValueError):
            start_date = None
            end_date = None
    elif scope == 'month':
        try:
            year = int(request.args.get('year') or today.year)
            month = int(request.args.get('month') or today.month)
            start_date = datetime(year, month, 1).date()
            end_date = datetime(year, 12, 31).date() if month == 12 else (
                datetime(year, month + 1, 1).date() - timedelta(days=1)
            )
        except (TypeError, ValueError):
            start_date = None
            end_date = None
    else:
        start_date = _parse_date(request.args.get('start'))
        end_date = _parse_date(request.args.get('end'))

    if not (start_date and end_date):
        start_date = today.replace(day=1)
        end_date = today

    if start_date < min_allowed_start:
        start_date = min_allowed_start
    if end_date < start_date:
        end_date = start_date

    return start_date, end_date


def _render_simple_statement_pdf(customer_obj, start_date, end_date):
    start_dt = datetime.combine(start_date, datetime.min.time()).replace(tzinfo=timezone.utc)
    end_dt = datetime.combine(end_date, datetime.max.time()).replace(tzinfo=timezone.utc)
    invoice_history = _get_customer_bill_history(
        customer_obj.id,
        start_dt=start_dt,
        end_dt=end_dt,
    )
    total_amount = round(sum(entry['total_amount'] for entry in invoice_history), 2)
    pdf_title = _build_export_pdf_title(
        customer_obj.company or customer_obj.name or customer_obj.phone or 'customer_statement',
        kind='simple_statement',
    )

    return render_template(
        'print_statement.html',
        start_date=start_date,
        end_date=end_date,
        total_invoices=len(invoice_history),
        total_amount=total_amount,
        inv_rows=[
            {
                'invoice_no': entry['invoice_no'],
                'date': entry['created_at'].strftime('%Y-%m-%d') if entry['created_at'] else '',
                'total': float(entry['total_amount'] or 0.0),
            }
            for entry in invoice_history
        ],
        customer_company=customer_obj.company or customer_obj.name or '(No Company)',
        customer_phone=customer_obj.phone or '',
        phone=customer_obj.phone or '',
        date_wise=False,
        APP_INFO=APP_INFO,
        pdf_title=pdf_title,
        generated_on=datetime.now(),
    )


def _render_company_simple_statement_pdf(start_date, end_date):
    start_dt = datetime.combine(start_date, datetime.min.time()).replace(tzinfo=timezone.utc)
    end_dt = datetime.combine(end_date, datetime.max.time()).replace(tzinfo=timezone.utc)
    context = _build_accounting_statement_context(
        start_dt,
        end_dt,
        start_date,
        end_date,
        txn_type_filter='all',
        customer_query='',
    )
    statement_invoices = context.get('statement_invoices') or []
    total_amount = round(sum(float(row.get('total') or 0.0) for row in statement_invoices), 2)
    pdf_title = _build_export_pdf_title(
        APP_INFO.get('business', {}).get('name') or 'company_statement',
        kind='company_statement',
    )

    return render_template(
        'print_statement.html',
        start_date=start_date,
        end_date=end_date,
        total_invoices=len(statement_invoices),
        total_amount=total_amount,
        inv_rows=[
            {
                'invoice_no': row['invoice_no'],
                'date': row['date'].strftime('%Y-%m-%d'),
                'total': float(row['total'] or 0.0),
                'company': row.get('company') or row.get('customer_name') or '',
                'phone': row.get('phone') or '',
            }
            for row in statement_invoices
        ],
        customer_company='',
        customer_phone='',
        phone='',
        date_wise=True,
        APP_INFO=APP_INFO,
        pdf_title=pdf_title,
        generated_on=datetime.now(),
    )


def _render_customer_accounting_statement_pdf(customer_obj, start_date, end_date):
    start_dt = datetime.combine(start_date, datetime.min.time()).replace(tzinfo=timezone.utc)
    end_dt = datetime.combine(end_date, datetime.max.time()).replace(tzinfo=timezone.utc)
    context = _build_accounting_statement_context(
        start_dt,
        end_dt,
        start_date,
        end_date,
        txn_type_filter='all',
        selected_customer_id=customer_obj.id,
    )
    template_payload = dict(context, APP_INFO=APP_INFO, statement_mode='accounting')
    pdf_label = customer_obj.company or customer_obj.name or customer_obj.phone or 'accounting_statement'
    template_payload['pdf_title'] = _build_export_pdf_title(pdf_label, kind='accounting_statement')
    return render_template('print_accounting_statement.html', **template_payload)


def _render_create_bill(**context):
    customer_obj = context.get('customer')
    if customer_obj and 'customer_bill_history' not in context:
        exclude_invoice_id = None
        if context.get('edit_mode'):
            exclude_invoice_id = context.get('invoice_no') or context.get('prev_invoice_no')
        context['customer_bill_history'] = _get_customer_bill_history(
            getattr(customer_obj, 'id', None),
            exclude_invoice_id=exclude_invoice_id,
        )
    else:
        context.setdefault('customer_bill_history', [])
    context.setdefault('show_prefilled_rows', bool(context.get('success') or context.get('edit_mode')))
    context.setdefault('rounded_flags', [])
    context['form_token'] = _issue_bill_token()
    return render_template('create_bill.html', **context)


def _ensure_file_writable(path: Path) -> None:
    """Best-effort to guarantee the SQLite file is writable (fixes Windows bundle perms)."""
    try:
        if not path.exists():
            return
        if os.name == "nt":
            path.chmod(stat.S_IWRITE | stat.S_IREAD)
        else:
            path.chmod(0o600)
    except Exception as exc:
        print(f"[warn] Could not adjust permissions for {path}: {exc}")


def _desktop_data_dir(app_name: str) -> Path:
    if os.name == "nt":
        return Path(os.getenv("APPDATA", str(Path.home() / "AppData" / "Roaming"))) / app_name
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / app_name
    return Path.home() / ".local" / "share" / app_name


BASE_DIR = Path(__file__).resolve().parent
IS_DESKTOP = os.getenv(BG_DESKTOP_ENV) == "1"
DATA_DIR = _desktop_data_dir(APP_NAME) if IS_DESKTOP else BASE_DIR / "db"
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / DATABASE_FILENAME
SEED_DB_PATH = BASE_DIR / "db" / DATABASE_FILENAME
INFO_PATH = DATA_DIR / INFO_FILENAME
SECRET_KEY_PATH = DATA_DIR / SECRET_KEY_FILENAME


def _resolve_secret_key() -> str:
    """Resolve a per-install secret key.

    Order of precedence:
      1. SECRET_KEY environment variable (best for containerized / CI use).
      2. A persisted random key under DATA_DIR/secret.key (created on
         first launch, 0600 permissions).
      3. The literal DEFAULT_SECRET_KEY (used only as a fallback when the
         above cannot be created — e.g. read-only filesystem). A warning
         is emitted in that case.
    """
    env_key = os.getenv("SECRET_KEY")
    if env_key:
        return env_key
    try:
        if SECRET_KEY_PATH.exists():
            existing = SECRET_KEY_PATH.read_text(encoding="utf-8").strip()
            if existing:
                return existing
        # Generate a fresh 32-byte URL-safe key.
        import secrets as _secrets
        new_key = _secrets.token_urlsafe(32)
        SECRET_KEY_PATH.write_text(new_key, encoding="utf-8")
        try:
            os.chmod(SECRET_KEY_PATH, 0o600)
        except OSError:
            pass
        return new_key
    except OSError as exc:
        print(
            f"WARNING: could not create persistent SECRET_KEY at {SECRET_KEY_PATH} ({exc}). "
            "Falling back to insecure default — set SECRET_KEY env var to silence this."
        )
        return DEFAULT_SECRET_KEY


app = Flask(__name__)
app.config["SECRET_KEY"] = _resolve_secret_key()
app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{DB_PATH.as_posix()}"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.register_blueprint(api_bp)

migrate_db(DB_PATH.as_posix())

# Attach to this Flask app
db.init_app(app)
migrate = Migrate(app, db)


def _format_customer_id(n: int) -> str:
    return f"ID-{n:06d}"


def format_inr(value, places: int = 2) -> str:
    """Return Indian-style grouping (e.g., 12,34,567.89)."""
    try:
        amt = Decimal(str(value or 0)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError, TypeError) as exc:
        logger.warning("format_inr: could not parse %r: %s", value, exc)
        amt = Decimal('0.00')

    sign = '-' if amt < 0 else ''
    amt = abs(amt)
    integer_part, frac = f"{amt:.{places}f}".split(".")

    if len(integer_part) > 3:
        last3 = integer_part[-3:]
        rest = integer_part[:-3]
        groups = []
        while rest:
            groups.append(rest[-2:])
            rest = rest[:-2]
        integer_part = ",".join(reversed(groups)) + "," + last3

    return f"{sign}{integer_part}.{frac}"


app.jinja_env.filters['inr'] = format_inr


def rounding_to_nearest_zero(amount):
    """Rounding number to nearest zero"""
    try:
        d = Decimal(str(amount))
    except (InvalidOperation, ValueError, TypeError) as exc:
        logger.warning("rounding_to_nearest_zero: could not parse %r: %s", amount, exc)
        d = Decimal('0')
    tens = (d / Decimal('10')).quantize(Decimal('1'), rounding=ROUND_HALF_UP)
    return float(tens * Decimal('10'))


def _ensure_db_initialized():
    """
    Ensure database schema exists.
    - If DB file missing: create schema (or copy seed if present + desktop).
    - If DB file exists but has no tables: create schema.
    """
    with app.app_context():
        # create file if missing (and optionally copy seed on desktop)
        if not DB_PATH.exists():
            try:
                if SEED_DB_PATH.exists() and IS_DESKTOP:
                    shutil.copy2(SEED_DB_PATH, DB_PATH)
                    print("[info] Copied seed DB to desktop data dir.")
                else:
                    # touch file so engine can open it cleanly
                    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
                    DB_PATH.touch(exist_ok=True)
                    print("[info] Created empty DB file.")
            except Exception as e:
                print(f"[warn] could not prepare DB file: {e}")
        # Always ensure the DB file is writable (pyinstaller seed copies can be read-only on Windows)
        _ensure_file_writable(DB_PATH)
        if DB_PATH.exists() and os.name == "nt":
            try:
                test_conn = sqlite3.connect(DB_PATH.as_posix())
                test_conn.execute("PRAGMA user_version;")
                test_conn.close()
            except sqlite3.OperationalError as exc:
                if "readonly" in str(exc).lower():
                    print("[warn] Detected read-only SQLite database; retrying permission reset.")
                    _ensure_file_writable(DB_PATH)
                else:
                    print(f"[warn] SQLite connection check failed: {exc}")

        # check whether tables exist
        insp = inspect(db.engine)
        tables = set(insp.get_table_names())

        if not REQUIRED_DB_TABLES.issubset(tables):
            print("[info] Creating/migrating schema via create_all()…")
            db.create_all()


def get_info_json_path():
    """Return correct info.json path"""
    return INFO_PATH


def ensure_info_json():
    """ensure db info.json exists or else creates it"""
    info_path = get_info_json_path()
    if not info_path.parent.exists():
        info_path.parent.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc)
    determined_start = _determine_data_start(now)
    start_iso = _format_iso_utc(determined_start)
    start_dt = _ensure_utc(datetime.strptime(start_iso, ISO_8601_UTC))
    created_display = _format_human_date(start_dt)

    default_sections = _default_info_sections(start_dt)
    default_sections["account_defaults"]["start_date"] = start_iso
    default_sections["meta"]["created_on"] = start_iso

    default_payload = {
        "created_on": created_display,
        "app_name": APP_NAME,
        "version": APP_VERSION,
        "last_updated": now.strftime(HUMAN_DATE_FMT),
        "onboarding_complete": False,
        "data": default_sections,
    }

    if not info_path.exists():
        try:
            with open(info_path, "w", encoding='utf-8') as f:
                json.dump(default_payload, f, indent=2, ensure_ascii=False)
            print("[info] Created info.json with default structure.")
        except Exception as e:
            print(f"[warn] could not create db info.json: {e}")
        return info_path

    try:
        with open(info_path, "r", encoding='utf-8') as f:
            info_data = json.load(f)
    except Exception as exc:
        print(f"[warn] Failed to read info.json ({exc}); rewriting with defaults.")
        try:
            with open(info_path, "w", encoding='utf-8') as f:
                json.dump(default_payload, f, indent=2, ensure_ascii=False)
        except Exception as write_err:
            print(f"[warn] could not rewrite db info.json: {write_err}")
        return info_path

    changed = False
    for key in ("created_on", "app_name", "version", "last_updated"):
        if key not in info_data:
            info_data[key] = default_payload[key]
            changed = True

    if "onboarding_complete" not in info_data:
        info_data["onboarding_complete"] = False
        changed = True

    if not isinstance(info_data.get("data"), dict):
        info_data["data"] = deepcopy(default_sections)
        changed = True
    else:
        if _merge_missing(info_data["data"], default_sections):
            changed = True

    data_section = info_data["data"]
    business_section = data_section.setdefault("business", {})
    if _sync_logo_color_settings(business_section):
        changed = True
    visual_section = data_section.setdefault("invoice_visual", {})
    if _sync_to_color_settings(visual_section):
        changed = True

    account_defaults = data_section.setdefault("account_defaults", {})
    existing_start = account_defaults.get("start_date")
    if existing_start:
        try:
            existing_start_dt = _ensure_utc(datetime.strptime(existing_start, ISO_8601_UTC))
        except Exception:
            account_defaults["start_date"] = start_iso
            changed = True
        else:
            if existing_start_dt > start_dt:
                account_defaults["start_date"] = start_iso
                changed = True
    else:
        account_defaults["start_date"] = start_iso
        changed = True

    meta_section = data_section.setdefault("meta", {})
    meta_created = meta_section.get("created_on")
    if meta_created:
        try:
            meta_dt = _ensure_utc(datetime.strptime(meta_created, ISO_8601_UTC))
        except Exception:
            meta_section["created_on"] = start_iso
            changed = True
        else:
            if meta_dt != start_dt:
                meta_section["created_on"] = start_iso
                changed = True
    else:
        meta_section["created_on"] = start_iso
        changed = True

    created_on_value = info_data.get("created_on")
    if created_on_value:
        try:
            created_on_dt = datetime.strptime(created_on_value, HUMAN_DATE_FMT).replace(tzinfo=timezone.utc)
        except Exception:
            info_data["created_on"] = created_display
            changed = True
        else:
            if created_on_dt.date() != start_dt.date():
                info_data["created_on"] = created_display
                changed = True
    else:
        info_data["created_on"] = created_display
        changed = True

    if changed:
        try:
            with open(info_path, "w", encoding='utf-8') as f:
                json.dump(info_data, f, indent=2, ensure_ascii=False)
        except Exception as exc:
            print(f"[warn] Failed to update info.json defaults: {exc}")

    return info_path


def loading_info():
    info_path = ensure_info_json()
    try:
        with open(info_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        logger.exception("Failed to load info.json at %s; using defaults", info_path)
        return {
            "app_name": APP_NAME,
            "version": APP_VERSION,
            "onboarding_complete": False,
            "data": _default_info_sections(),
        }


def refresh_info_json():
    """Reload the info.json without restarting the app"""
    global APP_INFO, ONBOARDING_COMPLETE
    try:
        full_payload = loading_info()
        new_info = full_payload.get('data', {})
        if not isinstance(new_info, dict):
            new_info = {}
        APP_INFO.clear()
        APP_INFO.update(new_info)
        ONBOARDING_COMPLETE = bool(full_payload.get('onboarding_complete', False))
    except Exception as e:
        print(f"[warn] Failed to load/refresh app_info: {e}")


_initial_info_payload = loading_info()
APP_INFO = _initial_info_payload.get('data', {})
if not isinstance(APP_INFO, dict):
    APP_INFO = {}
ONBOARDING_COMPLETE = bool(_initial_info_payload.get('onboarding_complete', False))


ONBOARDING_EXEMPT_ENDPOINTS = {
    'static',
    'onboarding',
    'onboarding_submit',
    'config_refresh',
}


def _is_onboarding_complete() -> bool:
    return bool(ONBOARDING_COMPLETE)


def _clean_analytics_payload(data: dict) -> dict:
    """Remove empty values and normalise strings from analytics payloads."""
    if not isinstance(data, dict):
        return {}

    cleaned = {}
    for key, value in data.items():
        if isinstance(value, str):
            value = value.strip()
        if value in (None, ""):
            continue
        cleaned[key] = value
    return cleaned


@app.before_request
def _enforce_onboarding_flow():
    if _is_onboarding_complete():
        return

    endpoint = request.endpoint or ''
    if endpoint in ONBOARDING_EXEMPT_ENDPOINTS:
        return
    if endpoint.startswith('api_bp.'):
        return

    return redirect(url_for('onboarding'))


@app.route('/onboarding', methods=['GET'])
def onboarding():
    if _is_onboarding_complete():
        return redirect(url_for('home'))

    seed_info = loading_info().get('data', {})
    business_defaults = seed_info.get('business', {}) if isinstance(seed_info, dict) else {}
    bank_defaults = seed_info.get('bank', {}) if isinstance(seed_info, dict) else {}
    return render_template(
        'onboarding.html',
        business=business_defaults,
        bank=bank_defaults,
    )


def _normalize_account_number(value: str) -> str:
    return ''.join(ch for ch in value if ch.isalnum())


def _get_upi_variants() -> List[Dict[str, str]]:
    """Return configured UPI variants with display metadata."""
    upi_info = APP_INFO.get('upi_info', {}) if isinstance(APP_INFO.get('upi_info'), dict) else {}
    business_info = APP_INFO.get('business', {}) if isinstance(APP_INFO.get('business'), dict) else {}

    def _clean(value: Optional[str]) -> Optional[str]:
        if not value:
            return None
        stripped = value.strip()
        return stripped or None

    variants: List[Dict[str, str]] = []

    primary_id = _clean(upi_info.get('upi_id') or business_info.get('upi_id'))
    primary_name = _clean(upi_info.get('upi_name') or business_info.get('upi_name') or business_info.get('owner'))
    if primary_id:
        variants.append({
            'key': 'primary',
            'label': 'Savings Account UPI',
            'upi_id': primary_id,
            'upi_name': primary_name or '',
        })

    current_id = _clean(upi_info.get('upi_current_id'))
    current_name = _clean(upi_info.get('upi_current_name') or primary_name)
    if current_id:
        variants.append({
            'key': 'current',
            'label': 'Current Account UPI',
            'upi_id': current_id,
            'upi_name': current_name or '',
        })

    return variants


def _find_upi_variant(choice: Optional[str]) -> Optional[Dict[str, str]]:
    if not choice:
        return None
    for variant in _get_upi_variants():
        if variant.get('key') == choice:
            return variant
    return None


def _format_upi_amount(value) -> Optional[str]:
    """Return a 2-decimal string amount for UPI (or None if invalid)."""
    if value is None or value == '':
        return None
    try:
        raw_str = str(value)
        normalized = raw_str.replace(',', '').replace('₹', '').replace('INR', '')
        normalized = re.sub(r'[^\d.\-]', '', normalized)
        if normalized.count('.') > 1:
            head, tail = normalized.split('.', 1)
            tail = tail.replace('.', '')
            normalized = f"{head}.{tail}"
        amount = Decimal(normalized).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return None
    if amount <= 0:
        return None
    return format(amount, 'f')


def _build_upi_qr_params(upi_id: str, amount, payee_name: Optional[str], currency: Optional[str] = None) -> Dict[str, str]:
    """Prepare consistent query params for the /api/generate_upi_qr endpoint."""
    params: Dict[str, str] = {"upi_id": upi_id}

    formatted_amount = _format_upi_amount(amount)
    if formatted_amount:
        params["am"] = formatted_amount

    cleaned_name = (payee_name or '').strip()
    if cleaned_name:
        params["pn"] = cleaned_name

    upi_currency = (currency or APP_INFO.get("upi_info", {}).get("currency") or "INR").strip().upper()
    if upi_currency:
        params["cu"] = upi_currency

    return params


@app.route('/onboarding/submit', methods=['POST'])
def onboarding_submit():
    if _is_onboarding_complete():
        flash('Onboarding already completed.', 'info')
        return redirect(url_for('home'))

    form = request.form

    def _clean(key: str) -> str:
        return (form.get(key) or '').strip()

    business_name = _clean('business_name')
    owner_name = _clean('owner_name')
    phone = _clean('phone')
    email = _clean('email')
    address = _clean('address')
    upi_id = _clean('upi_id')
    gstin = _clean('gstin').upper()
    business_type = _clean('business_type')
    pan = _clean('pan').upper()

    bank_account_number = _clean('bank_account_number')
    confirm_bank_account_number = _clean('confirm_bank_account_number')
    ifsc_code = _clean('ifsc_code').upper()
    account_holder_name = _clean('account_holder_name') or business_name
    branch_name = _clean('branch_name')
    bank_name = _clean('bank_name')

    skip_bank = form.get('skip_bank', 'false').lower() == 'true'

    errors: List[str] = []
    if not business_name:
        errors.append('Business name is required.')
    if not owner_name:
        errors.append('Owner name is required.')
    if not phone:
        errors.append('Phone number is required.')

    normalized_account = _normalize_account_number(bank_account_number)
    normalized_confirm = _normalize_account_number(confirm_bank_account_number)

    if not skip_bank:
        if not normalized_account:
            errors.append('Bank account number is required or choose skip.')
        elif normalized_account != normalized_confirm:
            errors.append('Bank account numbers do not match.')

    if errors:
        for err in errors:
            flash(err, 'danger')
        return redirect(url_for('onboarding'))

    now = datetime.now(timezone.utc)
    info_payload = loading_info()
    data_section = _default_info_sections(now)

    # Business details
    data_section['business'].update({
        'name': business_name,
        'owner': owner_name,
        'address': address,
        'email': email,
        'phone': phone,
        'gstin': gstin,
        'upi_id': upi_id,
        'upi_name': owner_name or business_name,
        'businessType': business_type,
        'pan': pan,
    })

    # Bank details (optional)
    if not skip_bank:
        data_section['bank'].update({
            'account_name': account_holder_name,
            'bank_name': bank_name,
            'branch': branch_name,
            'account_number': normalized_account,
            'ifsc': ifsc_code,
            'bhim': phone,
        })

    # Payment / UPI info
    data_section['upi_info'].update({
        'upi_id': upi_id,
        'upi_name': owner_name or business_name,
    })
    payment_methods = [m for m in data_section['payment'].get('methods', [])]
    if upi_id:
        if 'UPI' not in (method.upper() for method in payment_methods):
            payment_methods.insert(0, 'UPI')
        data_section['payment']['upi_qr_enabled'] = True
    else:
        payment_methods = [m for m in payment_methods if m.upper() != 'UPI'] or ['Bank Transfer']
        data_section['payment']['upi_qr_enabled'] = False
    data_section['payment']['methods'] = payment_methods

    # Account defaults / meta
    data_section['account_defaults']['start_date'] = now.strftime('%Y-%m-%dT%H:%M:%SZ')
    data_section['meta']['version'] = APP_VERSION
    data_section['meta']['created_on'] = now.strftime('%Y-%m-%dT%H:%M:%SZ')

    info_payload['data'] = data_section
    info_payload['onboarding_complete'] = True
    info_payload['last_updated'] = now.strftime('%d %B %Y')
    info_payload.setdefault('created_on', now.strftime('%d %B %Y'))
    info_payload.setdefault('app_name', APP_NAME)
    info_payload['version'] = APP_VERSION

    info_path = get_info_json_path()
    try:
        with open(info_path, 'w', encoding='utf-8') as f:
            json.dump(info_payload, f, indent=2, ensure_ascii=False)
    except Exception as exc:
        flash(f'Failed to save onboarding details: {exc}', 'danger')
        return redirect(url_for('onboarding'))

    refresh_info_json()
    flash('Setup complete! You can start generating invoices.', 'success')
    return redirect(url_for('home'))


def get_default_statement_start():
    """Return default statement start date from info.json"""
    tzinfo = tz.gettz(APP_INFO['account_defaults']['timezone'])
    return datetime.strptime(
        APP_INFO['account_defaults']['start_date'], '%Y-%m-%dT%H:%M:%SZ'
    ).replace(tzinfo=tzinfo)


# Call this AFTER importing models, so metadata is populated
with app.app_context():
    _ensure_db_initialized()


# --- routes continue below as usual ---

# Helpers for statement engine
def _parse_date(date_str):
    """Parse 'YYYY-MM-DD' into date or return None."""
    if not date_str:
        return None
    try:
        return datetime.strptime(date_str, '%Y-%m-%d').date()
    except Exception:
        return None


def _parse_int_arg(raw_value, *, min_value=None, max_value=None):
    """Parse string to int with optional bounds; returns (value, error_message)."""
    if raw_value in (None, ''):
        return None, None
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        return None, "must be a number"
    if min_value is not None and value < min_value:
        return None, f"must be ≥ {min_value}"
    if max_value is not None and value > max_value:
        return None, f"must be ≤ {max_value}"
    return value, None


@app.route('/recover')
def recover_page():
    deleted_customers = customer.query.filter_by(isDeleted=True).all()
    deleted_invoices = invoice.query.filter_by(isDeleted=True).all()
    deleted_transactions = (
        accountingTransaction.query
        .options(joinedload(accountingTransaction.customer))
        .filter(accountingTransaction.is_deleted.is_(True))
        .order_by(accountingTransaction.updated_at.desc())
        .all()
    )
    return render_template(
        'recover.html',
        deleted_customers=deleted_customers,
        deleted_invoices=deleted_invoices,
        deleted_transactions=deleted_transactions,
    )


@app.route('/recover_customer/<int:id>')
def recover_customer(id):
    cust = customer.query.get_or_404(id)
    cust.isDeleted = False
    if not _safe_commit('recover customer'):
        flash('Could not recover the customer. Please try again.', 'danger')
    else:
        flash('Customer recovered successfully.', 'success')
    return redirect(url_for('recover_page'))


@app.route('/more')
def more():
    supabase_meta = APP_INFO.get('supabase', {})
    last_sync_raw = supabase_meta.get('last_incremental_uploaded') or supabase_meta.get('last_uploaded')
    last_sync_display = _format_sync_timestamp(last_sync_raw)

    latest_backup = _latest_backup_path()
    if latest_backup:
        backup_iso = datetime.fromtimestamp(latest_backup.stat().st_mtime, tz=timezone.utc).isoformat()
        last_backup_display = _format_sync_timestamp(backup_iso)
        last_backup_name = latest_backup.name
    else:
        last_backup_display = "Never"
        last_backup_name = None

    return render_template(
        'more.html',
        has_backup_location=bool((APP_INFO.get('file_location') or '').strip()),
        last_sync_display=last_sync_display,
        last_backup_display=last_backup_display,
        last_backup_name=last_backup_name,
        APP_INFO=APP_INFO,
    )


@app.route('/analytics_event', methods=['GET', 'POST'])
def analytics_event():
    try:
        data = request.get_json(silent=True)

        if not data and request.form:
            data = request.form.to_dict(flat=True)

        if not data and request.data:
            try:
                data = json.loads(request.data.decode('utf-8') or '{}')
            except json.JSONDecodeError:
                data = {}

        normalized = _clean_analytics_payload(data or {})

        if not normalized:
            # Gracefully acknowledge empty analytics pings without treating them as errors
            return jsonify({"status": "ignored", "message": "No analytics payload supplied."}), 204

        # Call the logger in analytics_tracking.py
        log_user_event(normalized)

        return jsonify({"status": "success"}), 200
    except Exception as e:
        print(f"[warn] Analytics log failed: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/analytics')
def analytics():
    # Get sales trends for day, month, year, and weekday
    day_labels, day_totals = get_sales_trends("day")
    month_labels, month_totals = get_sales_trends("month")
    year_labels, year_totals = get_sales_trends("year")
    # Fetch weekday-level sales trends
    weekday_labels, weekday_totals = get_sales_trends("weekday")
    customer_names, revenues = get_top_customers()
    one_time, repeat = get_customer_retention()
    daywise_labels, daywise_counts, daywise_totals = get_day_wise_billing()

    return render_template(
        'analytics.html',
        day_labels=day_labels,
        day_totals=day_totals,
        month_labels=month_labels,
        month_totals=month_totals,
        year_labels=year_labels,
        year_totals=year_totals,
        weekday_labels=weekday_labels,
        weekday_totals=weekday_totals,
        customer_names=customer_names,
        revenues=revenues,
        one_time=one_time,
        repeat=repeat,
        daywise_labels=daywise_labels,
        daywise_counts=daywise_counts,
        daywise_totals=daywise_totals,
    )


@app.route('/about_user', methods=['GET', 'POST'])
def about_user():
    customer_id = request.args.get('customer_id', type=int)
    cust = customer.query.filter_by(id=customer_id, isDeleted=False).first() if customer_id else None
    if not cust:
        return _redirect_missing_customer(
            url_for('view_customers'),
            raw_target=request.args.get('next'),
        )

    data = {
        'id': cust.id,
        'name': cust.name,
        'email': cust.email,
        'company': cust.company,
        'phone': cust.phone,
        'gst': cust.gst,
        'address': cust.address,
        'businessType': cust.businessType
    }
    back_href = _safe_local_redirect(request.args.get('next') or request.referrer, url_for('view_customers'))
    return render_template(
        'about_user.html',
        data=data,
        app_info=APP_INFO,
        back_href=back_href,
        edit_href=url_for('edit_user', customer_id=cust.id, next=back_href),
        create_bill_href=url_for('start_bill', customer_id=cust.id),
        accounting_href=url_for('accounting_customer_detail', customer_id=cust.id),

    )


@app.route('/edit_user/<int:customer_id>', methods=['GET', 'POST'])
def edit_user(customer_id):
    cust = customer.query.filter_by(id=customer_id, isDeleted=False).first()
    if not cust:
        return _redirect_missing_customer(
            url_for('view_customers'),
            raw_target=request.args.get('next'),
        )

    next_url = _safe_local_redirect(
        request.values.get('next') or request.referrer,
        url_for('about_user', customer_id=customer_id),
    )

    if request.method == 'GET':
        return render_template('edit_user.html', customer=cust, next_url=next_url)

    # POST logic: update values
    name = request.form.get('name', '').strip()
    phone = request.form.get('phone', '').strip()
    address = request.form.get('address', '').strip()
    gst = request.form.get('gst', '').strip()
    email = request.form.get('email', '').strip()
    businessType = request.form.get('businessType', '').strip()
    company = request.form.get('company', '').strip()

    if not name or not phone:
        preview_customer = SimpleNamespace(
            id=cust.id,
            name=name,
            phone=phone,
            address=address,
            gst=gst,
            email=email,
            businessType=businessType,
            company=company,
        )
        return render_template(
            'edit_user.html',
            customer=preview_customer,
            next_url=next_url,
            error='Name and Phone are required fields.',
        )

    cust.name = name
    cust.phone = phone
    cust.address = address
    cust.gst = gst
    cust.email = email
    cust.businessType = businessType
    cust.company = company

    try:
        db.session.commit()
        flash('Customer updated successfully!', 'success')
    except Exception as e:
        db.session.rollback()
        preview_customer = SimpleNamespace(
            id=cust.id,
            name=name,
            phone=phone,
            address=address,
            gst=gst,
            email=email,
            businessType=businessType,
            company=company,
        )
        return render_template(
            'edit_user.html',
            customer=preview_customer,
            next_url=next_url,
            error=f'Error updating customer: {e}',
        )

    return redirect(_safe_local_redirect(next_url, url_for('about_user', customer_id=customer_id)))


@app.route('/recover_invoice/<int:id>')
def recover_invoice(id):
    inv = invoice.query.get_or_404(id)
    inv.isDeleted = False
    if not _safe_commit('recover invoice'):
        flash('Could not recover the invoice. Please try again.', 'danger')
    else:
        flash('Invoice recovered successfully.', 'success')
    return redirect(url_for('recover_page'))


@app.route('/recover_transaction/<int:txn_id>')
def recover_transaction(txn_id):
    txn = accountingTransaction.query.get_or_404(txn_id)
    txn.is_deleted = False
    if not _safe_commit('recover transaction'):
        flash('Could not recover the transaction. Please try again.', 'danger')
    else:
        flash('Transaction recovered successfully.', 'success')
    return redirect(url_for('recover_page'))


# Custom Jinja filter to format dates as DD Month YYYY (e.g., '14 October 2025')
@app.template_filter('datetimeformat')
def datetimeformat(value, format='%d %B %Y'):
    """Safely format a date string or datetime into a readable form (e.g., '14 October 2025')."""
    if not value:
        return ''
    try:
        if isinstance(value, str):
            # Try parsing both full and simple date formats
            for fmt in ('%Y-%m-%d', '%Y-%m-%d %H:%M:%S'):
                try:
                    value = datetime.strptime(value, fmt)
                    break
                except ValueError:
                    continue
        return value.strftime(format)
    except Exception:
        return str(value)


@app.route('/_flash_test')
def _flash_test():
    flash('Flash works!', 'success')
    return redirect(url_for('view_customers'))


# Home Route
@app.route('/')
def home():
    session['persistent_notice'] = None
    modal_context = _build_accounting_modal_context(next_url=url_for('home'))
    return render_template('home.html', **modal_context)


@app.route('/config', methods=['GET', 'POST'])
def config():
    info_path = get_info_json_path()
    layout_config = layoutConfig.get_or_create()
    layout_sizes = layout_config.get_sizes()

    # --- Load existing info.json (with fallback if corrupted/missing) ---
    info_data = loading_info()

    app_info = info_data.get("data", {})
    if not isinstance(app_info, dict):
        app_info = {}
    if 'file_location' not in app_info:
        app_info['file_location'] = ''
    # Merge legacy Cloud Settings block into supabase if present
    legacy_cloud = app_info.pop('Cloud Settings', None)
    if legacy_cloud:
        supabase_section = app_info.setdefault('supabase', {})
        if isinstance(legacy_cloud, dict):
            supabase_section.update(legacy_cloud)

    # --- Handle POST (Save Changes) ---
    if request.method == 'POST':
        section = request.form.get('section')
        if not section:
            flash('No section specified for update.', 'warning')
            return redirect(url_for('config'))

        # Get editable section data
        updates = {}
        for key, val in request.form.items():
            if key not in ('section',):
                updates[key] = val.strip()

        if section == 'supabase':
            instant_values = request.form.getlist('instant_uploads')
            if instant_values:
                raw_value = (instant_values[-1] or '').strip().lower()
                updates['instant_uploads'] = raw_value in ('true', '1', 'yes', 'on')

        # Apply updates to correct section
        if section == 'file_location':
            new_path = (updates.get('file_location') or updates.get('value') or '').strip()
            # Reject obviously dangerous targets — system roots and anything
            # outside the user's home directory. Empty string disables mirroring.
            if new_path:
                try:
                    resolved = Path(new_path).expanduser().resolve()
                    home = Path.home().resolve()
                    is_under_home = False
                    try:
                        resolved.relative_to(home)
                        is_under_home = True
                    except ValueError:
                        is_under_home = False
                    if not is_under_home:
                        flash(
                            "Backup mirror path must be inside your home directory.",
                            "danger",
                        )
                        return redirect(url_for('config'))
                except (OSError, RuntimeError) as exc:
                    flash(f"Invalid backup mirror path: {exc}", "danger")
                    return redirect(url_for('config'))
            app_info['file_location'] = new_path
        elif section == 'invoice_visual':
            business_section = app_info.setdefault('business', {})
            raw_color_mode = updates.get('logo_color_mode')
            if raw_color_mode:
                color_mode = _normalize_logo_color_mode(raw_color_mode)
                business_section['logo_color_mode'] = color_mode
                business_section['logo_path'] = LOGO_COLOR_PATHS[color_mode]

            visual_section = app_info.setdefault('invoice_visual', {})
            raw_to_mode = updates.get('to_color_mode')
            if raw_to_mode is not None:
                visual_section['to_color_mode'] = _normalize_to_color_mode(raw_to_mode)
            else:
                visual_section['to_color_mode'] = _normalize_to_color_mode(
                    visual_section.get('to_color_mode')
                )
            raw_to_custom = updates.get('to_color_custom')
            if raw_to_custom is not None:
                normalized_custom = _normalize_hex_color(raw_to_custom)
                if normalized_custom:
                    visual_section['to_color_custom'] = normalized_custom
                elif raw_to_custom.strip() == "":
                    visual_section['to_color_custom'] = ""
            _sync_to_color_settings(visual_section)

            size_fields = ('header', 'customer', 'invoice_info', 'table', 'totals', 'payment', 'footer')
            sizes_changed = False
            for field in size_fields:
                raw_value = request.form.get(field)
                if raw_value is None:
                    continue
                try:
                    new_value = int(raw_value)
                except (TypeError, ValueError):
                    continue
                if layout_sizes.get(field) != new_value:
                    layout_sizes[field] = new_value
                    sizes_changed = True
            if sizes_changed:
                layout_config.set_sizes(layout_sizes)
                db.session.commit()
        elif section in app_info:
            if isinstance(app_info[section], dict):
                target_section = app_info[section]
                filtered_updates = {}
                for key, new_value in updates.items():
                    existing_value = target_section.get(key)
                    if new_value == existing_value:
                        continue
                    if isinstance(existing_value, str) and new_value == existing_value:
                        continue
                    if new_value == '' and key in target_section:
                        continue
                    filtered_updates[key] = new_value
                if filtered_updates:
                    target_section.update(filtered_updates)
            elif isinstance(app_info[section], list):
                # handle lists (e.g., services textarea)
                lines = updates.get('services', '').splitlines()
                app_info[section] = [ln.strip() for ln in lines if ln.strip()]
            else:
                # simple scalar fields
                value = updates.get(section) or updates.get('value')
                app_info[section] = value.strip() if isinstance(value, str) else updates
        else:
            app_info[section] = updates

        # Update timestamp + save to file
        info_data['data'] = app_info
        info_data['last_updated'] = datetime.now(timezone.utc).strftime("%d %B %Y")

        try:
            with open(info_path, 'w', encoding='utf-8') as f:
                json.dump(info_data, f, indent=2, ensure_ascii=False)
            section_label = section.replace('_', ' ').title()
            flash(f"{section_label} updated successfully!", "success")
            refresh_info_json()
        except Exception as e:
            flash(f"Error saving changes: {e}", "danger")

        # Reload updated version
        return redirect(url_for('config'))

    # --- Default (GET) view ---
    return render_template('config_editor.html', app_info=app_info,
                           last_updated=info_data['last_updated'],
                           created_on=info_data['created_on'],
                           layout_sizes=layout_sizes)


@app.route('/config/refresh', methods=['POST'])
def config_refresh():
    try:
        refresh_info_json()
        flash('Account settings reloaded from info.json.', 'success')
    except Exception as exc:
        flash(f'Unable to refresh settings: {exc}', 'danger')
    return redirect(url_for('config'))


def _accounting_totals(sort_by='balance', sort_dir='desc'):
    base_query = db.session.query
    income_total = base_query(func.coalesce(func.sum(accountingTransaction.amount), 0.0)).filter(
        accountingTransaction.txn_type == 'income',
        accountingTransaction.is_deleted.is_(False)
    ).scalar() or 0.0

    expense_total = base_query(func.coalesce(func.sum(accountingTransaction.amount), 0.0)).filter(
        accountingTransaction.txn_type == 'expense',
        accountingTransaction.is_deleted.is_(False)
    ).scalar() or 0.0

    outstanding_invoice_rows = _outstanding_invoice_rows()
    general_payments = _general_customer_payments()
    customer_income_totals = _customer_income_totals()
    customer_expenses = _customer_expenses()
    outstanding_entries = _group_outstanding_by_customer(
        outstanding_invoice_rows,
        general_payments,
        customer_income_totals,
        customer_expenses,
        sort_by,
        sort_dir
    )
    outstanding_total = sum(entry['balance'] for entry in outstanding_entries)

    return {
        'income_total': income_total,
        'expense_total': expense_total,
        'net_flow': income_total - expense_total,
        'outstanding_total': outstanding_total,
        'outstanding_entries': outstanding_entries,
        'outstanding_invoices_raw': outstanding_invoice_rows,
        'sort_by': sort_by,
        'sort_dir': sort_dir,
    }


def _invoice_income_payments_subquery():
    return (
        db.session.query(
            accountingTransaction.invoice_no.label('invoice_no'),
            func.coalesce(func.sum(accountingTransaction.amount), 0.0).label('paid_amount')
        )
        .filter(
            accountingTransaction.is_deleted.is_(False),
            accountingTransaction.txn_type == 'income',
            accountingTransaction.invoice_no.isnot(None)
        )
        .group_by(accountingTransaction.invoice_no)
        .subquery()
    )


def _get_customer_due_candidates(current_invoice):
    payments_subq = _invoice_income_payments_subquery()
    rows = (
        db.session.query(
            invoice.invoiceId.label('invoice_no'),
            invoice.createdAt.label('created_at'),
            invoice.totalAmount.label('invoice_total'),
            func.coalesce(payments_subq.c.paid_amount, 0.0).label('paid_amount')
        )
        .outerjoin(payments_subq, payments_subq.c.invoice_no == invoice.invoiceId)
        .filter(
            invoice.customerId == current_invoice.customerId,
            invoice.isDeleted.is_(False),
            invoice.invoiceId != current_invoice.invoiceId
        )
        .order_by(invoice.createdAt.desc())
        .all()
    )

    due_rows = []
    for row in rows:
        balance = float(max((row.invoice_total or 0) - (row.paid_amount or 0), 0))
        if balance <= 0.01:
            continue
        due_rows.append({
            'invoice_no': row.invoice_no,
            'created_at': row.created_at,
            'date_label': row.created_at.strftime('%d %b %Y') if row.created_at else '',
            'amount': round(balance, 2),
            'invoice_total': float(round(row.invoice_total or 0, 2)),
            'paid_amount': float(round(row.paid_amount or 0, 2)),
        })
    return due_rows


def _build_due_summary_rows(current_invoice, selected_due_invoice_nos=None, *, include_current=False):
    due_candidates = _get_customer_due_candidates(current_invoice)
    candidate_map = {row['invoice_no']: row for row in due_candidates}

    selected_due_rows = []
    seen = set()
    for invoice_no in selected_due_invoice_nos or []:
        normalized = (invoice_no or '').strip()
        if not normalized or normalized in seen:
            continue
        row = candidate_map.get(normalized)
        if not row:
            continue
        selected_due_rows.append(row)
        seen.add(normalized)

    summary_rows = []
    if include_current:
        summary_rows.append({
            'invoice_no': current_invoice.invoiceId,
            'created_at': current_invoice.createdAt,
            'date_label': current_invoice.createdAt.strftime('%d %b %Y') if current_invoice.createdAt else '',
            'amount': float(round(current_invoice.totalAmount or 0, 2)),
            'is_current': True,
        })
    for row in selected_due_rows:
        summary_rows.append({
            'invoice_no': row['invoice_no'],
            'created_at': row['created_at'],
            'date_label': row['date_label'],
            'amount': row['amount'],
            'is_current': False,
        })

    summary_total = round(sum(row['amount'] for row in summary_rows), 2)
    return due_candidates, summary_rows, summary_total


def _get_invoice_outstanding_amount(invoice_obj) -> float:
    if not invoice_obj:
        return 0.0
    paid_amount = (
        db.session.query(func.coalesce(func.sum(accountingTransaction.amount), 0.0))
        .filter(
            accountingTransaction.invoice_no == invoice_obj.invoiceId,
            accountingTransaction.txn_type == 'income',
            accountingTransaction.is_deleted.is_(False)
        )
        .scalar()
        or 0.0
    )
    return round(max(float(invoice_obj.totalAmount or 0.0) - float(paid_amount or 0.0), 0.0), 2)


def _outstanding_invoice_rows():
    payments_subq = _invoice_income_payments_subquery()

    rows = (
        db.session.query(
            invoice.invoiceId.label('invoice_no'),
            invoice.createdAt,
            invoice.totalAmount,
            customer.id.label('customer_id'),
            customer.name.label('customer_name'),
            customer.company.label('customer_company'),
            func.coalesce(payments_subq.c.paid_amount, 0.0).label('paid_amount')
        )
        .join(customer, invoice.customerId == customer.id)
        .outerjoin(payments_subq, payments_subq.c.invoice_no == invoice.invoiceId)
        .filter(
            invoice.isDeleted.is_(False),
            or_(invoice.payment.is_(False), invoice.payment.is_(None))
        )
        .order_by(invoice.createdAt.desc())
    )

    invoice_rows = []
    for row in rows:
        balance = float(max((row.totalAmount or 0) - (row.paid_amount or 0), 0))
        if balance <= 0.01:
            continue
        invoice_rows.append({
            'invoice_no': row.invoice_no,
            'created_at': row.createdAt,
            'customer_id': row.customer_id,
            'customer': row.customer_name or row.customer_company or 'Customer',
            'company': row.customer_company,
            'total': float(row.totalAmount or 0),
            'paid': float(row.paid_amount or 0),
            'balance': balance,
        })
    return invoice_rows


def _general_customer_payments():
    rows = (
        db.session.query(
            accountingTransaction.customerId,
            func.coalesce(func.sum(accountingTransaction.amount), 0.0).label('amt')
        )
        .filter(
            accountingTransaction.is_deleted.is_(False),
            accountingTransaction.txn_type == 'income',
            accountingTransaction.invoice_no.is_(None),
            accountingTransaction.customerId.isnot(None)
        )
        .group_by(accountingTransaction.customerId)
        .all()
    )
    return {row.customerId: float(row.amt or 0) for row in rows}


def _customer_income_totals():
    rows = (
        db.session.query(
            accountingTransaction.customerId,
            func.coalesce(func.sum(accountingTransaction.amount), 0.0).label('amt')
        )
        .filter(
            accountingTransaction.is_deleted.is_(False),
            accountingTransaction.txn_type == 'income',
            accountingTransaction.customerId.isnot(None)
        )
        .group_by(accountingTransaction.customerId)
        .all()
    )
    return {row.customerId: float(row.amt or 0) for row in rows}


def _customer_expenses():
    rows = (
        db.session.query(
            accountingTransaction.customerId,
            func.coalesce(func.sum(accountingTransaction.amount), 0.0).label('amt')
        )
        .filter(
            accountingTransaction.is_deleted.is_(False),
            accountingTransaction.txn_type == 'expense',
            accountingTransaction.customerId.isnot(None)
        )
        .group_by(accountingTransaction.customerId)
        .all()
    )
    return {row.customerId: float(row.amt or 0) for row in rows}


def _group_outstanding_by_customer(
    invoice_rows,
    general_payments,
    customer_income_totals,
    customer_expenses,
    sort_by='balance',
    sort_dir='desc'
):
    grouped = {}
    for entry in invoice_rows:
        cust_id = entry.get('customer_id')
        key = cust_id or (entry['customer'], entry.get('company'))
        bucket = grouped.setdefault(key, {
            'customer_id': cust_id,
            'customer': entry['customer'],
            'company': entry.get('company'),
            'total': 0.0,
            'paid': 0.0,
            'paid_applied': 0.0,
            'expenses': 0.0,
            'invoice_count': 0,
            'latest_invoice_date': entry.get('created_at'),
        })
        bucket['total'] += entry['total']
        bucket['paid_applied'] += entry['paid']
        bucket['invoice_count'] += 1
        existing_date = bucket.get('latest_invoice_date')
        created_at = entry.get('created_at')
        if existing_date is None or (created_at and created_at > existing_date):
            bucket['latest_invoice_date'] = created_at
    existing_ids = {bucket['customer_id'] for bucket in grouped.values() if bucket.get('customer_id')}
    missing_expense_ids = [cid for cid in customer_expenses.keys() if cid and cid not in existing_ids]
    missing_lookup = {}
    if missing_expense_ids:
        customer_rows = customer.query.filter(customer.id.in_(missing_expense_ids)).all()
        missing_lookup = {row.id: row for row in customer_rows}
    for cid in missing_expense_ids:
        cust_obj = missing_lookup.get(cid)
        grouped[cid] = {
            'customer_id': cid,
            'customer': cust_obj.name if cust_obj else 'Customer',
            'company': cust_obj.company if cust_obj else None,
            'total': 0.0,
            'paid': 0.0,
            'paid_applied': 0.0,
            'expenses': 0.0,
            'invoice_count': 0,
            'latest_invoice_date': None,
        }

    for bucket in grouped.values():
        cust_id = bucket.get('customer_id')
        general = general_payments.get(cust_id)
        if general:
            bucket['paid_applied'] += general

        bucket['paid'] = customer_income_totals.get(cust_id, bucket.get('paid_applied', 0.0))

        expense_sum = customer_expenses.get(cust_id)
        if expense_sum:
            bucket['expenses'] += expense_sum

        total_due = bucket['total'] + bucket['expenses']
        bucket['balance'] = max(total_due - bucket.get('paid_applied', 0.0), 0.0)

    result = list(grouped.values())
    sort_key_map = {
        'invoices': lambda r: r['invoice_count'],
        'balance': lambda r: r['balance'],
        'expenses': lambda r: r['expenses'],
        'paid': lambda r: r['paid'],
        'invoiced': lambda r: r['total'],
    }
    sort_field = sort_key_map.get(sort_by, sort_key_map['balance'])
    reverse = (sort_dir.lower() != 'asc')
    result.sort(key=sort_field, reverse=reverse)
    return result


def _ensure_business_expense_customer():
    cust = (
        customer.query
        .filter(
            customer.name == "Business Expense",
            customer.isDeleted == False
        )
        .first()
    )
    if cust:
        return cust
    synthetic_phone = f"EXP-{int(datetime.now(timezone.utc).timestamp())}"
    cust = customer(
        name="Business Expense",
        company="Internal Ledger",
        phone=synthetic_phone,
        email="",
        gst="",
        address="",
        businessType="Expense",
        isDeleted=False
    )
    db.session.add(cust)
    db.session.flush()
    return cust


def _persist_accounting_transaction(form, existing_txn=None):
    previous_invoice_no = existing_txn.invoice_no if existing_txn else None
    previous_txn_type = existing_txn.txn_type if existing_txn else None
    txn_type = (form.get('txn_type') or 'income').strip().lower()
    if txn_type not in ('income', 'expense'):
        txn_type = 'income'

    customer_id_val = form.get('customer_id')
    customer_obj = None
    if customer_id_val:
        try:
            customer_obj = db.session.get(customer, int(customer_id_val))
        except (TypeError, ValueError):
            customer_obj = None
        if not customer_obj or customer_obj.isDeleted:
            return "Selected customer could not be found."

    customer_name = None
    if txn_type == 'income' and not customer_obj:
        return "Select a customer for payments received."
    if txn_type == 'expense' and not customer_obj:
        customer_obj = _ensure_business_expense_customer()

    selected_invoice_nos = []
    seen_invoices = set()
    if not existing_txn and txn_type == 'income':
        for raw_invoice_no in form.getlist('selected_invoice_no[]'):
            invoice_code = (raw_invoice_no or '').strip()
            if not invoice_code or invoice_code in seen_invoices:
                continue
            selected_invoice_nos.append(invoice_code)
            seen_invoices.add(invoice_code)

    invoice_no = (form.get('invoice_no') or '').strip() or None
    invoice_obj = None
    if invoice_no and not selected_invoice_nos:
        invoice_obj = invoice.query.filter_by(invoiceId=invoice_no).first()
        if not invoice_obj:
            return "Invoice number could not be located."
        if customer_obj and invoice_obj.customerId != customer_obj.id:
            return "Invoice does not belong to the selected customer."

    txn_created_at = None
    txn_date_raw = (form.get('txn_date') or '').strip()
    if txn_date_raw:
        try:
            tz_name = (APP_INFO.get('account_defaults') or {}).get('timezone') or DEFAULT_TIMEZONE
            local_tz = tz.gettz(tz_name) or timezone.utc
            parsed_date = datetime.strptime(txn_date_raw, '%Y-%m-%d')
            local_dt = datetime(parsed_date.year, parsed_date.month, parsed_date.day, 12, 0, tzinfo=local_tz)
            txn_created_at = local_dt.astimezone(timezone.utc)
        except Exception:
            txn_created_at = None

    txn_kwargs = {}
    if txn_created_at:
        txn_kwargs['created_at'] = txn_created_at
        txn_kwargs['updated_at'] = txn_created_at

    mode_value = (form.get('mode') or '').strip().lower() or 'cash'
    account_value = (form.get('account') or '').strip().lower() or 'cash'
    remarks_value = (form.get('remarks') or '').strip() or None

    amount_raw = (form.get('amount') or '').strip()
    amount_decimal = None
    if not selected_invoice_nos or existing_txn:
        try:
            amount_decimal = Decimal(amount_raw)
        except (InvalidOperation, TypeError):
            return "Enter a valid amount."
        if amount_decimal <= 0:
            return "Amount must be greater than zero."

    txn = existing_txn
    if selected_invoice_nos:
        invoice_rows = (
            invoice.query
            .filter(
                invoice.invoiceId.in_(selected_invoice_nos),
                invoice.isDeleted.is_(False),
            )
            .all()
        )
        invoice_map = {inv.invoiceId: inv for inv in invoice_rows}
        invoices_to_sync = set()

        for invoice_code in selected_invoice_nos:
            selected_invoice = invoice_map.get(invoice_code)
            if not selected_invoice:
                return "One or more selected bills could not be found."
            if customer_obj and selected_invoice.customerId != customer_obj.id:
                return "One or more selected bills do not belong to the selected customer."

            outstanding_amount = _get_invoice_outstanding_amount(selected_invoice)
            if outstanding_amount <= 0.01:
                return f"Invoice {invoice_code} no longer has any outstanding balance."

            db.session.add(accountingTransaction(
                customerId=customer_obj.id if customer_obj else None,
                amount=float(outstanding_amount),
                txn_type=txn_type,
                mode=mode_value,
                account=account_value,
                invoice_no=invoice_code,
                remarks=remarks_value,
                **txn_kwargs
            ))
            invoices_to_sync.add(invoice_code)
    else:
        if txn:
            txn.customerId = customer_obj.id if customer_obj else None
            txn.amount = float(amount_decimal)
            txn.txn_type = txn_type
            txn.mode = mode_value
            txn.account = account_value
            txn.invoice_no = invoice_no
            txn.remarks = remarks_value
            if txn_created_at:
                txn.created_at = txn_created_at
            txn.updated_at = datetime.now(timezone.utc)
            db.session.flush()
        else:
            txn = accountingTransaction(
                customerId=customer_obj.id if customer_obj else None,
                amount=float(amount_decimal),
                txn_type=txn_type,
                mode=mode_value,
                account=account_value,
                invoice_no=invoice_no,
                remarks=remarks_value,
                **txn_kwargs
            )
            db.session.add(txn)
            db.session.flush()

        if existing_txn:
            for entry in list(existing_txn.expense_items):
                db.session.delete(entry)

        if txn_type == 'expense':
            descriptions = form.getlist('expense_desc[]')
            amounts = form.getlist('expense_amount[]')
            for idx, desc in enumerate(descriptions):
                desc_text = (desc or '').strip()
                if not desc_text:
                    continue
                amount_val = None
                if idx < len(amounts):
                    amt_raw = (amounts[idx] or '').strip()
                    if amt_raw:
                        try:
                            amount_val = float(Decimal(amt_raw))
                        except (InvalidOperation, TypeError):
                            amount_val = None
                expense_entry = expenseItem(
                    transactionId=txn.id,
                    description=desc_text,
                    amount=amount_val
                )
                db.session.add(expense_entry)

        invoices_to_sync = set()
        if txn_type == 'income' and invoice_no:
            invoices_to_sync.add(invoice_no)
        if previous_txn_type == 'income' and previous_invoice_no:
            invoices_to_sync.add(previous_invoice_no)

    for inv_code in invoices_to_sync:
        _sync_invoice_payment_flag(inv_code)

    try:
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        return f"Unable to record transaction: {exc}"
    return None


def _sync_invoice_payment_flag(invoice_code: Optional[str]):
    if not invoice_code:
        return
    target_invoice = invoice.query.filter_by(invoiceId=invoice_code, isDeleted=False).first()
    if not target_invoice:
        return
    paid_amount = (
        db.session.query(func.coalesce(func.sum(accountingTransaction.amount), 0.0))
        .filter(
            accountingTransaction.invoice_no == invoice_code,
            accountingTransaction.txn_type == 'income',
            accountingTransaction.is_deleted.is_(False)
        )
        .scalar()
        or 0.0
    )
    invoice_total = float(target_invoice.totalAmount or 0.0)
    target_invoice.payment = paid_amount >= max(invoice_total, 0.0) - 0.01


# Accounting dashboard
@app.route('/accounting', methods=['GET', 'POST'])
def accounting_dashboard():
    if request.method == 'POST':
        next_url = (request.form.get('next_url') or '').strip()
        fallback_next = url_for('accounting_dashboard')
        parsed_next = urlparse(next_url)
        allowed_exact_paths = {
            url_for('home'),
            url_for('accounting_dashboard'),
            url_for('accounting_transactions_list'),
            url_for('accounting_statement'),
        }
        allowed_prefixes = (
            '/accounting/customer/',
        )
        if (
            not next_url
            or (parsed_next.netloc and parsed_next.netloc != request.host)
            or (
                (parsed_next.path or '') not in allowed_exact_paths
                and not any((parsed_next.path or '').startswith(prefix) for prefix in allowed_prefixes)
            )
        ):
            next_url = fallback_next
        error = _persist_accounting_transaction(request.form)
        if error:
            db.session.rollback()
            flash(error, 'danger')
        else:
            selected_invoices = [
                (invoice_code or '').strip()
                for invoice_code in request.form.getlist('selected_invoice_no[]')
                if (invoice_code or '').strip()
            ]
            flash(
                'Payments recorded successfully.' if selected_invoices else 'Transaction recorded successfully.',
                'success',
            )
        return redirect(next_url)

    search_query = (request.args.get('customer') or '').strip()
    if search_query:
        matched_customer = _resolve_accounting_customer_search(search_query)
        if matched_customer:
            return redirect(url_for('accounting_customer_detail', customer_id=matched_customer.id))

    totals = _accounting_totals(sort_by='balance', sort_dir='desc')
    outstanding = totals['outstanding_entries']
    top_due_customers = outstanding[:3]
    customers_list = customer.alive().order_by(customer.name.asc()).all()
    suggestions = [
        {
            'name': cust.name or '',
            'company': cust.company or '',
            'phone': cust.phone or '',
        }
        for cust in customers_list
        if cust.name or cust.company or cust.phone
    ]

    payment_modes = ['cash', 'bank', 'upi']
    account_options = ['cash', 'savings', 'current']
    business_expense_id = _ensure_business_expense_customer().id

    return render_template(
        'accounting.html',
        totals=totals,
        top_due_customers=top_due_customers,
        customers_with_dues=len(outstanding),
        customers=customers_list,
        payment_modes=payment_modes,
        account_options=account_options,
        business_expense_id=business_expense_id,
        customer_search=search_query,
        search_error='No customer matched that search.' if search_query else '',
        suggestions=suggestions,
    )


@app.route('/accounting/quick_clear/<int:customer_id>', methods=['POST'])
def accounting_quick_clear(customer_id):
    cust = customer.query.filter_by(id=customer_id, isDeleted=False).first()
    if not cust:
        flash('Customer not found.', 'warning')
        return redirect(url_for('accounting_dashboard'))

    snapshot = _customer_financial_snapshot(customer_id)
    balance = float(snapshot.get('balance') or 0.0)
    if balance <= 0:
        flash('No outstanding balance to clear for this customer.', 'info')
        return redirect(url_for('accounting_dashboard'))

    txn = accountingTransaction(
        customerId=customer_id,
        amount=balance,
        txn_type='income',
        mode='bank',
        account='current',
        remarks='Quick clear via dashboard',
    )
    db.session.add(txn)
    cleared_invoices = (
        invoice.query
        .filter(invoice.customerId == customer_id, invoice.isDeleted.is_(False))
        .all()
    )
    for inv in cleared_invoices:
        inv.payment = True
    try:
        db.session.commit()
        flash(f"Cleared INR: {balance:,.2f} for {cust.name}.", 'success')
    except Exception as exc:
        db.session.rollback()
        flash(f"Unable to clear dues: {exc}", 'danger')

    return redirect(url_for('accounting_dashboard'))


@app.route('/accounting/transactions')
def accounting_transactions_list():
    sort_by = (request.args.get('sort') or 'id').lower()
    sort_dir = (request.args.get('dir') or 'desc').lower()
    if sort_by not in {'date', 'amount', 'customer', 'type', 'id'}:
        sort_by = 'id'
    if sort_dir not in {'asc', 'desc'}:
        sort_dir = 'desc'

    customer_query = (request.args.get('customer') or '').strip()
    date_query = (request.args.get('date') or '').strip()
    amount_query = (request.args.get('amount') or '').strip()

    q = (
        accountingTransaction.query
        .outerjoin(customer)
        .options(joinedload(accountingTransaction.customer))
        .filter(accountingTransaction.is_deleted.is_(False))
    )

    if customer_query:
        like_value = f"%{customer_query.lower()}%"
        q = q.filter(
            or_(
                func.lower(customer.name).like(like_value),
                func.lower(customer.company).like(like_value),
                func.lower(accountingTransaction.txn_id).like(like_value)
            )
        )

    if date_query:
        try:
            parsed_date = datetime.strptime(date_query, '%Y-%m-%d')
            start = parsed_date.replace(hour=0, minute=0, second=0, microsecond=0)
            end = start + timedelta(days=1)
            q = q.filter(accountingTransaction.created_at >= start, accountingTransaction.created_at < end)
        except Exception:
            pass

    if amount_query:
        try:
            amt_value = float(Decimal(amount_query))
            q = q.filter(accountingTransaction.amount == amt_value)
        except (InvalidOperation, ValueError):
            pass

    if sort_by == 'amount':
        sort_column = accountingTransaction.amount
    elif sort_by == 'customer':
        sort_column = func.lower(customer.name)
    elif sort_by == 'type':
        sort_column = accountingTransaction.txn_type
    elif sort_by == 'id':
        sort_column = accountingTransaction.id
    else:
        sort_column = accountingTransaction.created_at

    if sort_dir == 'asc':
        q = q.order_by(sort_column.asc(), accountingTransaction.id.asc())
    else:
        q = q.order_by(sort_column.desc(), accountingTransaction.id.desc())

    transactions = q.all()

    customers_list = customer.alive().order_by(customer.name.asc()).all()
    payment_modes = ['cash', 'bank', 'upi']
    account_options = ['cash', 'savings', 'current']
    business_expense_id = _ensure_business_expense_customer().id

    return render_template(
        'accounting_transactions.html',
        transactions=transactions,
        filter_customer=customer_query,
        filter_date=date_query,
        filter_amount=amount_query,
        current_sort=sort_by,
        current_dir=sort_dir,
        customers=customers_list,
        payment_modes=payment_modes,
        account_options=account_options,
        business_expense_id=business_expense_id,
    )


@app.route('/accounting/transactions/<int:txn_id>', methods=['GET', 'POST'])
def accounting_transaction_detail(txn_id):
    txn = (
        accountingTransaction.query
        .options(joinedload(accountingTransaction.customer), joinedload(accountingTransaction.expense_items))
        .get_or_404(txn_id)
    )

    if request.method == 'POST':
        if txn.is_deleted:
            flash('Transaction already archived.', 'warning')
        else:
            txn.is_deleted = True
            invoice_code = txn.invoice_no if txn.txn_type == 'income' else None
            _sync_invoice_payment_flag(invoice_code)
            db.session.commit()
            flash('Transaction deleted successfully.', 'success')
        return redirect(url_for('accounting_transactions_list'))

    return render_template('accounting_transaction_detail.html', txn=txn)


@app.route('/accounting/transactions/<int:txn_id>/edit', methods=['GET', 'POST'])
def accounting_transaction_edit(txn_id):
    txn = (
        accountingTransaction.query
        .options(joinedload(accountingTransaction.customer), joinedload(accountingTransaction.expense_items))
        .get_or_404(txn_id)
    )
    if txn.is_deleted:
        flash('Cannot edit an archived transaction.', 'warning')
        return redirect(url_for('accounting_transaction_detail', txn_id=txn_id))

    customers_list = customer.alive().order_by(customer.name.asc()).all()
    invoice_choices = []
    seen = set()
    for row in _outstanding_invoice_rows():
        inv = row.get('invoice_no')
        if inv and inv not in seen:
            seen.add(inv)
            invoice_choices.append(inv)
    if txn.invoice_no and txn.invoice_no not in invoice_choices:
        invoice_choices.append(txn.invoice_no)
    payment_modes = ['cash', 'bank', 'upi']
    account_options = ['cash', 'savings', 'current']
    business_expense_id = _ensure_business_expense_customer().id

    form_data = None
    expense_rows = []

    def _build_expense_rows(source_form):
        if not source_form:
            rows = [
                {
                    'desc': (item.description or ''),
                    'amount': '' if item.amount is None else f"{item.amount:.2f}"
                }
                for item in txn.expense_items
            ]
        else:
            descs = source_form.getlist('expense_desc[]')
            amts = source_form.getlist('expense_amount[]')
            rows = []
            for idx in range(max(len(descs), len(amts))):
                desc_val = descs[idx] if idx < len(descs) else ''
                amt_val = amts[idx] if idx < len(amts) else ''
                rows.append({'desc': desc_val, 'amount': amt_val})
        if not rows:
            rows = [{'desc': '', 'amount': ''}]
        return rows

    if request.method == 'POST':
        form_data = request.form
        error = _persist_accounting_transaction(request.form, existing_txn=txn)
        if error:
            db.session.rollback()
            flash(error, 'danger')
            expense_rows = _build_expense_rows(form_data)
        else:
            flash('Transaction updated successfully.', 'success')
            return redirect(url_for('accounting_transaction_detail', txn_id=txn.id))
    else:
        expense_rows = _build_expense_rows(None)

    return render_template(
        'accounting_transaction_edit.html',
        txn=txn,
        customers=customers_list,
        invoice_choices=invoice_choices,
        payment_modes=payment_modes,
        account_options=account_options,
        business_expense_id=business_expense_id,
        form_data=form_data,
        expense_rows=expense_rows
    )


def _customer_financial_snapshot(customer_id: int) -> dict:
    total_invoiced, invoice_count = (
        db.session.query(
            func.coalesce(func.sum(invoice.totalAmount), 0.0),
            func.count(invoice.id)
        )
        .filter(
            invoice.customerId == customer_id,
            invoice.isDeleted.is_(False)
        )
        .one()
    )

    total_payments = (
        db.session.query(func.coalesce(func.sum(accountingTransaction.amount), 0.0))
        .filter(
            accountingTransaction.customerId == customer_id,
            accountingTransaction.txn_type == 'income',
            accountingTransaction.is_deleted.is_(False)
        )
        .scalar()
        or 0.0
    )

    total_expenses = (
        db.session.query(func.coalesce(func.sum(accountingTransaction.amount), 0.0))
        .filter(
            accountingTransaction.customerId == customer_id,
            accountingTransaction.txn_type == 'expense',
            accountingTransaction.is_deleted.is_(False)
        )
        .scalar()
        or 0.0
    )

    balance = (total_invoiced + total_expenses) - total_payments

    return {
        'customer_id': customer_id,
        'total_invoiced': float(total_invoiced or 0.0),
        'invoice_count': int(invoice_count or 0),
        'total_payments': float(total_payments or 0.0),
        'total_expenses': float(total_expenses or 0.0),
        'balance': float(balance),
    }


def _build_accounting_statement_context(
    start_dt: datetime,
    end_dt: datetime,
    start_date_display,
    end_date_display,
    txn_type_filter: str = 'all',
    customer_query: str = '',
    selected_customer_id: Optional[int] = None,
) -> dict:
    """
    Aggregate accounting transactions for the printable accounting statement view.
    Returns totals, breakdowns, and the matching transaction rows.
    """
    txn_filter = txn_type_filter if txn_type_filter in {'income', 'expense'} else 'all'
    customer_filter = (customer_query or '').strip()

    tz_name = (APP_INFO.get('account_defaults') or {}).get('timezone') or DEFAULT_TIMEZONE
    display_tz = tz.gettz(tz_name) or timezone.utc

    selected_customer = None
    if selected_customer_id:
        selected_customer = customer.alive().filter_by(id=selected_customer_id).first()
    elif customer_filter:
        selected_customer = _resolve_statement_customer_token(customer_filter)

    q = (
        accountingTransaction.query
        .options(joinedload(accountingTransaction.customer))
        .filter(
            accountingTransaction.is_deleted.is_(False),
            accountingTransaction.created_at >= start_dt,
            accountingTransaction.created_at <= end_dt,
        )
    )

    if txn_filter in {'income', 'expense'}:
        q = q.filter(accountingTransaction.txn_type == txn_filter)

    if selected_customer:
        q = q.outerjoin(customer, accountingTransaction.customerId == customer.id)
        q = q.filter(accountingTransaction.customerId == selected_customer.id)
    else:
        q = q.outerjoin(customer, accountingTransaction.customerId == customer.id)

    transactions = q.order_by(accountingTransaction.created_at.desc()).all()

    income_total = 0.0
    expense_total = 0.0
    income_count = 0
    expense_count = 0
    daily_totals = defaultdict(lambda: {'income': 0.0, 'expense': 0.0})
    mode_totals = defaultdict(lambda: {'income': 0.0, 'expense': 0.0})
    account_totals = defaultdict(lambda: {'income': 0.0, 'expense': 0.0})
    customer_totals = {}

    for txn in transactions:
        amount = float(txn.amount or 0.0)
        if amount <= 0:
            continue
        created_at = txn.created_at or start_dt
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        local_created = created_at.astimezone(display_tz)
        date_key = local_created.date()
        day_bucket = daily_totals[date_key]

        if txn.txn_type == 'income':
            income_total += amount
            income_count += 1
            day_bucket['income'] += amount
        else:
            expense_total += amount
            expense_count += 1
            day_bucket['expense'] += amount

        mode_label = (txn.mode or 'Unspecified').strip() or 'Unspecified'
        account_label = (txn.account or 'Unspecified').strip() or 'Unspecified'
        mode_bucket = mode_totals[mode_label.upper()]
        account_bucket = account_totals[account_label.title()]
        if txn.txn_type == 'income':
            mode_bucket['income'] += amount
            account_bucket['income'] += amount
        else:
            mode_bucket['expense'] += amount
            account_bucket['expense'] += amount

        cust_key = txn.customerId if txn.customerId else 'unassigned'
        customer_bucket = customer_totals.get(cust_key)
        if not customer_bucket:
            label = txn.customer.name if txn.customer else 'Unassigned'
            customer_bucket = {
                'customer_id': txn.customer.id if txn.customer else None,
                'customer': label,
                'company': txn.customer.company if txn.customer else '',
                'phone': txn.customer.phone if txn.customer else '',
                'income': 0.0,
                'expense': 0.0,
                'transactions': 0,
                'last_txn_at': local_created,
            }
            customer_totals[cust_key] = customer_bucket

        customer_bucket['transactions'] += 1
        if txn.txn_type == 'income':
            customer_bucket['income'] += amount
        else:
            customer_bucket['expense'] += amount

        if local_created > customer_bucket['last_txn_at']:
            customer_bucket['last_txn_at'] = local_created

    def _format_breakdown(source):
        rows = []
        for label, values in source.items():
            total = values['income'] + values['expense']
            rows.append({
                'label': label,
                'income': values['income'],
                'expense': values['expense'],
                'net': values['income'] - values['expense'],
                'total': total,
            })
        rows.sort(key=lambda row: row['total'], reverse=True)
        return rows

    customer_summary = []
    for entry in customer_totals.values():
        entry['net'] = entry['income'] - entry['expense']
        customer_summary.append(entry)
    customer_summary.sort(
        key=lambda row: (row['customer_id'] is None, -row['net'])
    )

    daily_summary = []
    for day, values in sorted(daily_totals.items()):
        daily_summary.append({
            'date': day,
            'income': values['income'],
            'expense': values['expense'],
            'net': values['income'] - values['expense'],
        })

    show_income_columns = income_total > 0.0
    show_expense_columns = expense_total > 0.0

    statement_invoices = []
    customer_invoices = []
    customer_payments = []
    customer_adjustments = []
    customer_statement_summary = None
    selected_customer_info = None

    invoice_query = (
        invoice.query
        .options(joinedload(invoice.customer))
        .filter(
            invoice.isDeleted == False,
            invoice.createdAt >= start_dt,
            invoice.createdAt <= end_dt
        )
    )
    if selected_customer:
        invoice_query = invoice_query.filter(invoice.customerId == selected_customer.id)

    invoice_rows = invoice_query.order_by(invoice.createdAt.desc(), invoice.id.desc()).all()
    for inv in invoice_rows:
        inv_created = inv.createdAt or start_dt
        if inv_created.tzinfo is None:
            inv_created = inv_created.replace(tzinfo=timezone.utc)
        local_inv = inv_created.astimezone(display_tz)
        statement_invoices.append({
            'invoice_no': inv.invoiceId,
            'date': local_inv,
            'total': float(inv.totalAmount or 0),
            'customer_name': inv.customer.name if inv.customer else '',
            'company': inv.customer.company if inv.customer else '',
            'phone': inv.customer.phone if inv.customer else '',
            'is_paid': bool(getattr(inv, 'payment', False)),
        })

    if selected_customer:
        selected_customer_info = {
            'id': selected_customer.id,
            'name': selected_customer.name,
            'company': selected_customer.company,
            'phone': selected_customer.phone,
        }
        customer_invoices = [
            {
                'invoice_no': inv['invoice_no'],
                'date': inv['date'],
                'total': inv['total'],
            }
            for inv in statement_invoices
        ]

        for txn in transactions:
            if txn.customerId != selected_customer.id:
                continue
            local_created = (txn.created_at or start_dt)
            if local_created.tzinfo is None:
                local_created = local_created.replace(tzinfo=timezone.utc)
            local_created = local_created.astimezone(display_tz)
            entry = {
                'txn_id': txn.txn_id,
                'date': local_created,
                'mode': txn.mode or '—',
                'account': txn.account or '—',
                'invoice_no': txn.invoice_no,
                'amount': float(txn.amount or 0),
                'remarks': txn.remarks,
            }
            if txn.txn_type == 'income':
                customer_payments.append(entry)
            else:
                customer_adjustments.append(entry)

        invoice_total = sum(inv['total'] for inv in customer_invoices)
        payment_total = sum(p['amount'] for p in customer_payments)
        adjustment_total = sum(adj['amount'] for adj in customer_adjustments)
        customer_statement_summary = {
            'invoice_total': round(invoice_total, 2),
            'payment_total': round(payment_total, 2),
            'adjustment_total': round(adjustment_total, 2),
            'balance': round(invoice_total + adjustment_total - payment_total, 2),
            'invoice_count': len(customer_invoices),
            'payment_count': len(customer_payments),
        }

    context = {
        'transactions': transactions,
        'income_total': round(income_total, 2),
        'expense_total': round(expense_total, 2),
        'net_flow': round(income_total - expense_total, 2),
        'transaction_count': len(transactions),
        'income_count': income_count,
        'expense_count': expense_count,
        'filters': {
            'start': start_date_display.isoformat(),
            'end': end_date_display.isoformat(),
            'customer': customer_filter,
            'type': txn_filter,
        },
        'customer_filter': customer_filter,
        'selected_type': txn_filter,
        'customer_summary': customer_summary,
        'mode_breakdown': _format_breakdown(mode_totals),
        'account_breakdown': _format_breakdown(account_totals),
        'daily_summary': daily_summary,
        'display_timezone': tz_name,
        'generated_at': datetime.now(timezone.utc).astimezone(display_tz),
        'start_date': start_date_display,
        'end_date': end_date_display,
        'start_local': start_dt.astimezone(display_tz),
        'end_local': end_dt.astimezone(display_tz),
        'overall_totals': _accounting_totals(),
        'statement_invoices': statement_invoices,
        'statement_invoice_total': round(sum(inv['total'] for inv in statement_invoices), 2),
        'statement_invoice_count': len(statement_invoices),
        'customer_invoices': customer_invoices,
        'selected_customer': selected_customer_info,
        'customer_payments': customer_payments,
        'customer_adjustments': customer_adjustments,
        'customer_statement_summary': customer_statement_summary,
        'show_income_columns': show_income_columns,
        'show_expense_columns': show_expense_columns,
    }
    context['statement_range_label'] = (
        f"{context['start_local'].strftime('%d %b %Y')} — {context['end_local'].strftime('%d %b %Y')}"
    )
    return context


@app.route('/accounting/customer_summary/<int:customer_id>')
def accounting_customer_summary(customer_id):
    cust = customer.query.filter_by(id=customer_id, isDeleted=False).first()
    if not cust:
        return jsonify({'error': 'Customer not found'}), 404
    summary = _customer_financial_snapshot(customer_id)
    summary['customer_name'] = cust.name
    summary['company'] = cust.company
    return jsonify(summary)


@app.route('/accounting/customer_invoices/<int:customer_id>')
def accounting_customer_invoices(customer_id):
    cust = customer.query.filter_by(id=customer_id, isDeleted=False).first()
    if not cust:
        return jsonify({'error': 'Customer not found'}), 404
    return jsonify({
        'customer_id': cust.id,
        'customer_name': cust.name,
        'company': cust.company,
        'invoices': _get_customer_transaction_invoice_rows(cust.id),
    })


@app.route('/accounting/customer/<int:customer_id>')
def accounting_customer_detail(customer_id):
    cust = customer.query.filter_by(id=customer_id, isDeleted=False).first_or_404()

    raw_start = request.args.get('start')
    raw_end = request.args.get('end')
    bounds = _get_customer_activity_date_bounds(cust.id)

    start_date = _parse_date(raw_start) or bounds['start_date']
    end_date = _parse_date(raw_end) or datetime.now(timezone.utc).date()
    if end_date < start_date:
        end_date = start_date

    context = _build_accounting_customer_page_context(
        cust,
        start_date=start_date,
        end_date=end_date,
        filters_active=bool(raw_start or raw_end),
    )
    modal_next_url = request.full_path[:-1] if request.full_path.endswith('?') else request.full_path
    context.update(_build_accounting_modal_context(
        preset_customer_id=cust.id,
        next_url=modal_next_url,
    ))
    context['mark_paid_redirect'] = modal_next_url

    return render_template(
        'accounting_customer.html',
        APP_INFO=APP_INFO,
        **context,
    )


@app.route('/accounting/customer/<int:customer_id>/simple-statement')
def accounting_customer_simple_statement(customer_id):
    cust = customer.query.filter_by(id=customer_id, isDeleted=False).first_or_404()
    bounds = _get_customer_activity_date_bounds(cust.id)

    start_date = _parse_date(request.args.get('start')) or bounds['start_date']
    end_date = _parse_date(request.args.get('end')) or datetime.now(timezone.utc).date()
    if end_date < start_date:
        end_date = start_date

    return _render_simple_statement_pdf(cust, start_date, end_date)


@app.route('/accounting/customer/<int:customer_id>/statement')
def accounting_customer_statement(customer_id):
    cust = customer.query.filter_by(id=customer_id, isDeleted=False).first_or_404()
    bounds = _get_customer_activity_date_bounds(cust.id)

    start_date = _parse_date(request.args.get('start')) or bounds['start_date']
    end_date = _parse_date(request.args.get('end')) or datetime.now(timezone.utc).date()
    if end_date < start_date:
        end_date = start_date

    return _render_customer_accounting_statement_pdf(cust, start_date, end_date)


@app.route('/accounting/amount_to_words')
def accounting_amount_to_words():
    amount = request.args.get('amount', '0')
    return jsonify({'words': amount_to_words(amount)})


# customers page (temperory placeholder)
@app.route('/create_customers', methods=['GET', 'POST'])
def add_customers():
    def _render_add_customer(**context):
        context.setdefault('next_url', (request.form.get('next_url') or request.args.get('next_url') or '').strip())
        context.setdefault(
            'bill_generation',
            _draft_flag_enabled(request.form.get('bill_generation') or request.args.get('bill_generation'))
        )
        return render_template('add_customer.html', **context)

    if request.method == 'POST':
        use_auto = bool(request.form.get('use_auto_id'))
        phone = (request.form.get('phone') or '').strip()
        name = (request.form.get('name') or '').strip()
        company = (request.form.get('company') or '').strip()
        email = (request.form.get('email') or '').strip()
        gst = (request.form.get('gst') or '').strip()
        address = (request.form.get('address') or '').strip()
        businessType = (request.form.get('businessType') or '').strip()

        # --- Basic validation ---
        if not name or (not use_auto and not phone):
            return _render_add_customer(
                success=False,
                duplicate=False,
                error='Name is required. Phone is required unless you use computer-generated ID.',
                # sticky values
                name=name, company=company, phone=phone, email=email,
                gst=gst, address=address, businessType=businessType,
                use_auto_id=use_auto
            )

        # --- Duplicate checks (exclude soft-deleted if you have that flag) ---
        not_deleted = getattr(customer, 'isDeleted', False) == False

        # 1) Phone duplicate (only when not using auto-id)
        if not use_auto and phone:
            existing_phone = (customer.query
                              .filter(func.lower(customer.phone) == phone.lower(), not_deleted)
                              .first())
            if existing_phone:
                return _render_add_customer(
                    duplicate=True,
                    error='A customer with this phone already exists.',
                    name=name, company=company, phone=phone, email=email,
                    gst=gst, address=address, businessType=businessType,
                    use_auto_id=use_auto
                )

        # 2) Company+Name duplicate (case-insensitive)
        if company and name:
            existing_pair = (customer.query
                             .filter(func.lower(customer.company) == company.lower(),
                                     func.lower(customer.name) == name.lower(),
                                     not_deleted)
                             .first())
            if existing_pair:
                return _render_add_customer(
                    duplicate=True,
                    error='A customer with the same Company + Name already exists.',
                    name=name, company=company, phone=phone, email=email,
                    gst=gst, address=address, businessType=businessType,
                    use_auto_id=use_auto
                )

        # --- Create customer ---
        if not use_auto and phone:
            # Real phone path
            c = customer(
                name=name, company=company, phone=phone, email=email,
                gst=gst, address=address, businessType=businessType
            )
            db.session.add(c)
            db.session.commit()
            # add alert
            flash('New Customer Created successfully.', 'success')
            return redirect(_post_create_customer_redirect(c))

        # Auto-ID path (no real phone or toggle checked)
        temp_phone = f"ID-TEMP-{uuid.uuid4().hex[:8]}"
        c = customer(
            name=name, company=company, phone=temp_phone, email=email,
            gst=gst, address=address, businessType=businessType
        )

        db.session.add(c)
        db.session.flush()  # get c.id
        c.phone = _format_customer_id(c.id)  # e.g., ID-000123
        db.session.commit()
        # add alert
        flash('New Customer Created successfully.', 'success')
        return redirect(_post_create_customer_redirect(c))

    # GET -> render blank form
    return _render_add_customer()


@app.route('/delete_customer/<int:cid>', methods=['GET', 'POST'])
def delete_customer(cid):
    # Load customer
    c = db.session.get(customer, cid)
    if not c:
        flash("Customer not found", 'warning')
        return redirect(url_for('view_customers'))

    # If already deleted, bail gracefully
    if hasattr(c, 'isDeleted') and c.isDeleted:
        flash('Customer already deleted.', 'info')
        return redirect(url_for('view_customers'))

    # Live invoices (exclude soft-deleted)
    inv_q = invoice.query.filter(
        invoice.customerId == cid,
        getattr(invoice, 'isDeleted', False) == False
    )
    inv_count = inv_q.count()
    total_billed = db.session.query(
        func.coalesce(func.sum(invoice.totalAmount), 0.0)
    ).filter(
        invoice.customerId == cid,
        getattr(invoice, 'isDeleted', False) == False
    ).scalar() or 0.0

    # If POST with confirm flag -> cascade soft-delete customer + invoices
    if request.method == 'POST' and request.form.get('confirm') == '1':
        if hasattr(c, 'isDeleted'):
            c.isDeleted = True
        # Soft delete all invoices for this customer
        invs = invoice.query.filter_by(customerId=cid).all()
        for inv in invs:
            if hasattr(inv, 'isDeleted'):
                inv.isDeleted = True
                inv.deletedAt = datetime.now(timezone.utc)
        if not _safe_commit('cascade delete customer'):
            flash('Could not delete the customer. Please try again.', 'danger')
            return redirect(url_for('view_customers'))
        flash('Customer and related invoices deleted successfully.', 'danger')
        return redirect(url_for('view_customers'))

    # GET: If invoices exist but not confirmed yet -> show confirm page
    if request.method == 'GET' and inv_count > 0:
        return render_template(
            'confirm_delete_customer.html',
            customer=c,
            inv_count=inv_count,
            total_billed=total_billed
        )

    # No invoices -> delete immediately (GET or POST)
    if hasattr(c, 'isDeleted'):
        c.isDeleted = True
        if not _safe_commit('delete customer'):
            flash('Could not delete the customer. Please try again.', 'danger')
        else:
            flash('Customer deleted.', 'danger')
    else:
        flash('Delete not available in this build.', 'warning')

    return redirect(url_for('view_customers'))


@app.route('/add_inventory', methods=['GET', 'POST'])
def add_inventory():
    # this function will be used to create a new inventory item.
    if request.method == 'POST':
        # Read and normalize inputs
        name = (request.form.get('name') or '').strip()
        unit_price_raw = (request.form.get('unitPrice') or '').strip()
        qty_raw = (request.form.get('quantity') or '').strip()
        tax_raw = (request.form.get('taxPercentage') or '').strip()

        # Basic validation
        if not name:
            return render_template('add_inventory.html', success=False, error='Item name is required.')

        try:
            unit_price = float(unit_price_raw or 0)
        except ValueError:
            return render_template('add_inventory.html', success=False, error='Unit price must be a number.')

        try:
            qty = int(qty_raw or 10000)
        except ValueError:
            return render_template('add_inventory.html', success=False, error='Quantity must be an integer.')

        try:
            tax_pct = float(tax_raw or 0)
        except ValueError:
            return render_template('add_inventory.html', success=False, error='Tax % must be a number.')

        # Duplicate check by name (case-insensitive)
        existing = item.query.filter(func.lower(item.name) == name.lower()).first()
        if existing:
            return render_template('add_inventory.html', duplicate=True)

        # Create item; SKU auto-assigned by model's before_insert listener
        new_item = item(
            name=name,
            unitPrice=unit_price,
            quantity=qty,
            taxPercentage=tax_pct
        )
        db.session.add(new_item)
        db.session.commit()
        # add alert
        flash('Item added successfully.', 'success')
        return render_template('add_inventory.html', success=True)

    return render_template('add_inventory.html')


@app.route('/select_customer', methods=['GET', 'POST'])
def select_customer():
    if request.method == 'POST':
        # User clicked Select on a customer row; the form sends phone
        phone = request.form.get('customer')
        sel = (customer.query
               .filter(customer.isDeleted == False, customer.phone == phone)
               .first_or_404())
        inventory_list = item.query.order_by(item.name.asc()).all()
        return _render_create_bill(customer=sel, inventory=inventory_list)

    # GET: either search or show recent
    q = (request.args.get('q') or '').strip()
    base = customer.query.filter(customer.isDeleted == False)
    sort_key = func.lower(func.coalesce(customer.company, customer.name, ''))
    if q:
        like = f"%{q}%"
        customers = (base.filter((customer.phone.ilike(like)) |
                                 (customer.name.ilike(like)) |
                                 (customer.company.ilike(like)))
                     .order_by(sort_key.asc(), customer.id.asc())
                     .all())
    else:
        customers = (base.order_by(sort_key.asc(), customer.id.asc())
                     .all())

    draft_counts = _get_active_draft_counts([cust.id for cust in customers])
    return render_template('select_customer.html', customers=customers, draft_counts=draft_counts)


@app.route('/view_inventory')
def view_inventory():
    query = (request.args.get('q') or '').lower()
    inventory = item.query.all()

    if query:
        inventory = [
            it for it in inventory
            if query in it.name.lower() or (it.sku is not None and query in str(it.sku).lower())
        ]

    return render_template('view_inventory.html', inventory=inventory)


@app.route('/api/statements', methods=['GET'])
def api_statements_summary():
    """JSON summary for dashboards.
    Query params same as /statements (scope/year/month/start/end/phone).
    Returns: {range, totals, per_customer, per_day, per_month}
    """
    # Delegate date-range parsing to the /statements logic by reusing code
    scope = (request.args.get('scope') or 'custom').lower()
    phone = request.args.get('phone')
    today = datetime.now().date()
    if scope == 'year':
        year_raw = request.args.get('year')
        year, err = _parse_int_arg(year_raw, min_value=2000, max_value=2100)
        if err or year is None:
            return jsonify({"error": "Invalid year. Please provide a number between 2000 and 2100."}), 400
        start_date = datetime(year, 1, 1).date()
        end_date = datetime(year, 12, 31).date()
    elif scope == 'month':
        year_raw = request.args.get('year')
        month_raw = request.args.get('month')
        year, y_err = _parse_int_arg(year_raw, min_value=2000, max_value=2100)
        month, m_err = _parse_int_arg(month_raw, min_value=1, max_value=12)
        if y_err or year is None:
            return jsonify({"error": "Invalid year. Please provide a number between 2000 and 2100."}), 400
        if m_err or month is None:
            return jsonify({"error": "Invalid month. Please provide a number between 1 and 12."}), 400
        start_date = datetime(year, month, 1).date()
        end_date = datetime(year, 12, 31).date() if month == 12 else (
                datetime(year, month + 1, 1).date() - timedelta(days=1))
    else:
        start_date = _parse_date(request.args.get('start'))
        end_date = _parse_date(request.args.get('end'))
        if not (start_date and end_date):
            return jsonify({"error": "Provide start and end in YYYY-MM-DD for custom scope"}), 400

    start_dt = datetime.combine(start_date, datetime.min.time())
    end_dt = datetime.combine(end_date, datetime.max.time())

    q = (invoice.query
         .options(joinedload(invoice.customer))
         .join(customer, invoice.customerId == customer.id)
         .filter(invoice.isDeleted == False,
                 customer.isDeleted == False,
                 invoice.createdAt >= start_dt,
                 invoice.createdAt <= end_dt))

    if phone:
        q = q.filter(customer.phone == phone)
    invs = q.order_by(invoice.createdAt.asc()).all()

    totals = {
        "invoice_count": len(invs),
        "amount": round(sum((inv.totalAmount or 0) for inv in invs), 2)
    }

    per_customer = defaultdict(lambda: {"count": 0, "amount": 0.0})
    per_day = defaultdict(lambda: {"count": 0, "amount": 0.0})
    per_month = defaultdict(lambda: {"count": 0, "amount": 0.0})
    for inv in invs:
        cust = inv.customer
        cust_key = f"{cust.name} ({cust.phone})" if cust else "Unknown"
        per_customer[cust_key]["count"] += 1
        per_customer[cust_key]["amount"] += (inv.totalAmount or 0)
        dkey = inv.createdAt.strftime('%Y-%m-%d')
        per_day[dkey]["count"] += 1
        per_day[dkey]["amount"] += (inv.totalAmount or 0)
        mkey = inv.createdAt.strftime('%Y-%m')
        per_month[mkey]["count"] += 1
        per_month[mkey]["amount"] += (inv.totalAmount or 0)

    # Convert defaultdicts to plain dicts for JSON
    return jsonify({
        "range": {"start": start_date.isoformat(), "end": end_date.isoformat()},
        "totals": totals,
        "per_customer": {k: {"count": v["count"], "amount": round(v["amount"], 2)} for k, v in per_customer.items()},
        "per_day": {k: {"count": v["count"], "amount": round(v["amount"], 2)} for k, v in per_day.items()},
        "per_month": {k: {"count": v["count"], "amount": round(v["amount"], 2)} for k, v in per_month.items()},
    })


@app.route('/api/statements/invoices', methods=['GET'])
def api_statements_invoices():
    """JSON: raw invoice rows with pagination (for tables/exports).
    Query params:
      scope/year/month/start/end/phone (same as above)
      page (default 1), per_page (default 50, max 500)
    """
    scope = (request.args.get('scope') or 'custom').lower()
    phone = request.args.get('phone')
    page = max(int(request.args.get('page', 1)), 1)
    per_page = min(max(int(request.args.get('per_page', 50)), 1), 500)

    today = datetime.now().date()
    if scope == 'year':
        year_raw = request.args.get('year')
        year, err = _parse_int_arg(year_raw, min_value=2000, max_value=2100)
        if err or year is None:
            return jsonify({"error": "Invalid year. Please provide a number between 2000 and 2100."}), 400
        start_date = datetime(year, 1, 1).date()
        end_date = datetime(year, 12, 31).date()
    elif scope == 'month':
        year_raw = request.args.get('year')
        month_raw = request.args.get('month')
        year, y_err = _parse_int_arg(year_raw, min_value=2000, max_value=2100)
        month, m_err = _parse_int_arg(month_raw, min_value=1, max_value=12)
        if y_err or year is None:
            return jsonify({"error": "Invalid year. Please provide a number between 2000 and 2100."}), 400
        if m_err or month is None:
            return jsonify({"error": "Invalid month. Please provide a number between 1 and 12."}), 400
        start_date = datetime(year, month, 1).date()
        end_date = datetime(year, 12, 31).date() if month == 12 else (
                datetime(year, month + 1, 1).date() - timedelta(days=1))
    else:
        start_date = _parse_date(request.args.get('start'))
        end_date = _parse_date(request.args.get('end'))
        if not (start_date and end_date):
            return jsonify({"error": "Provide start and end in YYYY-MM-DD for custom scope"}), 400

    start_dt = datetime.combine(start_date, datetime.min.time())
    end_dt = datetime.combine(end_date, datetime.max.time())

    q = (invoice.query
         .options(joinedload(invoice.customer))
         .join(customer, invoice.customerId == customer.id)
         .filter(invoice.isDeleted == False,
                 customer.isDeleted == False,
                 invoice.createdAt >= start_dt,
                 invoice.createdAt <= end_dt))

    if phone:
        q = q.filter(customer.phone == phone)

    total = q.count()
    invs = q.order_by(invoice.createdAt.asc()).offset((page - 1) * per_page).limit(per_page).all()

    rows = []
    for inv in invs:
        cust = inv.customer
        rows.append({
            "invoice_no": inv.invoiceId,
            "date": inv.createdAt.strftime('%Y-%m-%d'),
            "customer": cust.name if cust else 'Unknown',
            "phone": cust.phone if cust else '',
            "total": round(inv.totalAmount or 0, 2)
        })

    return jsonify({
        "range": {"start": start_date.isoformat(), "end": end_date.isoformat()},
        "total": total,
        "page": page,
        "per_page": per_page,
        "rows": rows
    })


@app.route('/statements/blank', methods=['GET'])
def statements_blank():
    return redirect(url_for('accounting_statement'))


@app.route('/bill-drafts')
def bill_drafts():
    search_query = (request.args.get('q') or '').strip()
    customer_id = request.args.get('customer_id', type=int)

    drafts_query = (
        billDraft.query
        .options(joinedload(billDraft.customer))
        .join(customer, customer.id == billDraft.customerId)
        .filter(
            billDraft.status == 'draft',
            customer.isDeleted.is_(False),
        )
    )

    selected_customer = None
    if customer_id:
        selected_customer = customer.alive().filter_by(id=customer_id).first()
        if selected_customer:
            drafts_query = drafts_query.filter(billDraft.customerId == selected_customer.id)
        else:
            flash('Customer not found for draft filter.', 'warning')
            return redirect(url_for('bill_drafts'))

    if search_query:
        like_value = f"%{search_query}%"
        drafts_query = drafts_query.filter(
            or_(
                customer.name.ilike(like_value),
                customer.company.ilike(like_value),
                customer.phone.ilike(like_value),
            )
        )

    drafts = (
        drafts_query
        .order_by(billDraft.updatedAt.desc(), billDraft.id.desc())
        .all()
    )

    return render_template(
        'bill_drafts.html',
        drafts=drafts,
        search_query=search_query,
        customer_filter=selected_customer,
    )


@app.route('/bill-drafts/<int:draft_id>')
def open_bill_draft(draft_id):
    draft_record = (
        billDraft.query
        .options(joinedload(billDraft.customer))
        .filter(
            billDraft.id == draft_id,
            billDraft.status == 'draft',
        )
        .first_or_404()
    )
    selected_customer = draft_record.customer
    if not selected_customer or selected_customer.isDeleted:
        flash('This draft belongs to a deleted customer and can no longer be opened.', 'warning')
        return redirect(url_for('bill_drafts'))

    draft_context = _build_bill_draft_form_context(draft_record)
    inventory_list = item.query.order_by(item.name.asc()).all()
    return _render_create_bill(
        customer=selected_customer,
        inventory=inventory_list,
        draft_mode=True,
        draft_id=draft_record.id,
        draft_updated_at=draft_record.updatedAt,
        **draft_context,
    )


@app.route('/bill-drafts/save', methods=['POST'])
def save_bill_draft():
    selected_phone = (request.form.get('customer_phone') or request.form.get('customer') or '').strip()
    selected_customer = customer.alive().filter_by(phone=selected_phone).first()
    if not selected_customer:
        flash('Please select a valid customer before saving a draft.', 'warning')
        return redirect(url_for('select_customer'))

    draft_payload = _serialize_bill_draft_payload(request.form)
    draft_id = request.form.get('draft_id', type=int)

    if draft_id:
        draft_record = (
            billDraft.query
            .filter(
                billDraft.id == draft_id,
                billDraft.status == 'draft',
            )
            .first_or_404()
        )
        draft_record.customerId = selected_customer.id
        draft_record.payloadJson = draft_payload['payload_json']
        draft_record.totalAmount = draft_payload['total_amount']
        draft_record.itemCount = draft_payload['item_count']
        draft_record.updatedAt = datetime.now(timezone.utc)
        flash('Draft updated successfully.', 'success')
    else:
        draft_record = billDraft(
            customerId=selected_customer.id,
            status='draft',
            payloadJson=draft_payload['payload_json'],
            totalAmount=draft_payload['total_amount'],
            itemCount=draft_payload['item_count'],
            createdAt=datetime.now(timezone.utc),
            updatedAt=datetime.now(timezone.utc),
        )
        db.session.add(draft_record)
        flash('Draft saved successfully.', 'success')

    db.session.commit()
    return redirect(url_for('open_bill_draft', draft_id=draft_record.id))


@app.route('/bill-drafts/<int:draft_id>/archive', methods=['POST'])
def archive_bill_draft(draft_id):
    draft_record = (
        billDraft.query
        .filter(
            billDraft.id == draft_id,
            billDraft.status == 'draft',
        )
        .first_or_404()
    )
    draft_record.status = 'archived'
    draft_record.updatedAt = datetime.now(timezone.utc)
    db.session.commit()
    flash('Draft removed from active drafts.', 'success')

    fallback = url_for('bill_drafts', customer_id=draft_record.customerId)
    return redirect(_safe_local_redirect(request.form.get('next'), fallback))


@app.route('/bill-drafts/archive-bulk', methods=['POST'])
def archive_bill_drafts_bulk():
    scope = (request.form.get('scope') or 'all').strip().lower()
    fallback = url_for('bill_drafts')
    redirect_target = _safe_local_redirect(request.form.get('next'), fallback)

    drafts_query = (
        billDraft.query
        .join(customer, customer.id == billDraft.customerId)
        .filter(
            billDraft.status == 'draft',
            customer.isDeleted.is_(False),
        )
    )

    if scope == 'customer':
        customer_id = request.form.get('customer_id', type=int)
        selected_customer = customer.alive().filter_by(id=customer_id).first()
        if not selected_customer:
            flash('Customer not found for draft cleanup.', 'warning')
            return redirect(redirect_target)
        drafts_query = drafts_query.filter(billDraft.customerId == selected_customer.id)
    else:
        selected_customer = None

    drafts = drafts_query.all()
    if not drafts:
        flash('No active drafts found for this action.', 'info')
        return redirect(redirect_target)

    now = datetime.now(timezone.utc)
    for draft_record in drafts:
        draft_record.status = 'archived'
        draft_record.updatedAt = now

    db.session.commit()
    if selected_customer:
        flash(f'Archived {len(drafts)} active draft(s) for {selected_customer.company or selected_customer.name}.', 'success')
    else:
        flash(f'Archived {len(drafts)} active draft(s).', 'success')
    return redirect(redirect_target)


@app.route('/bills/<invoice_no>/duplicate-draft', methods=['POST'])
def duplicate_bill_as_draft(invoice_no):
    source_invoice = invoice.query.filter_by(invoiceId=invoice_no, isDeleted=False).first_or_404()
    draft_payload = _build_bill_draft_payload_from_invoice(source_invoice)
    draft_record = billDraft(
        customerId=source_invoice.customerId,
        status='draft',
        payloadJson=draft_payload['payload_json'],
        totalAmount=draft_payload['total_amount'],
        itemCount=draft_payload['item_count'],
        createdAt=datetime.now(timezone.utc),
        updatedAt=datetime.now(timezone.utc),
    )
    db.session.add(draft_record)
    db.session.commit()
    flash(f'Draft created from invoice {source_invoice.invoiceId}.', 'success')
    return redirect(url_for('open_bill_draft', draft_id=draft_record.id))


@app.route('/create-bill', methods=['GET', 'POST'])
def start_bill():
    # --- GET: prefill if customer_id is supplied; otherwise show selector ---
    if request.method == 'GET':
        cid = request.args.get('customer_id', type=int)
        if cid:
            try:
                cust = (customer.query
                        .filter(customer.isDeleted == False, customer.id == cid)
                        .first())
            except Exception:
                cust = None
            if not cust:
                return _redirect_missing_customer(
                    url_for('select_customer'),
                    message='This customer no longer exists. Please choose another customer.',
                )
            inventory_list = item.query.order_by(item.name.asc()).all()
            return _render_create_bill(customer=cust, inventory=inventory_list)
        # GET: no customer_id, just render blank/new bill
        return _render_create_bill()

    # POST logic
    # POST (A) select customer
    if 'description[]' not in request.form:
        selected_phone = request.form.get("customer") or request.form.get("customer_phone")
        sel = (customer.query
               .filter(customer.isDeleted == False, customer.phone == selected_phone)
               .first())
        if not sel:
            flash('Please pick a valid customer', 'warning')
            return redirect(url_for('select_customer'))
        inventory_list = item.query.order_by(item.name.asc()).all()
        return _render_create_bill(customer=sel, inventory=inventory_list)

    # (B) Final bill submission with line items
    submitted_token = request.form.get('form_token')
    if not _validate_bill_token(submitted_token):
        flash('The bill form has expired. Please start a new bill.', 'warning')
        return redirect(url_for('select_customer'))

    selected_phone = request.form.get('customer_phone')
    selected_customer = customer.query.filter_by(phone=selected_phone).first()
    if not selected_customer:
        flash('Customer not found. Please reselect the customer.', 'warning')
        return render_template('select_customer.html')

    descriptions = request.form.getlist('description[]')
    quantities = request.form.getlist('quantity[]')
    rates = request.form.getlist('rate[]')
    dc_numbers = request.form.getlist('dc_no[]')  # may be [] if toggle off
    rounded_flags = request.form.getlist('rounded[]')

    # Get exclusion flags
    exclude_phone = bool(request.form.get('exclude_phone'))
    exclude_gst = bool(request.form.get('exclude_gst'))
    exclude_addr = bool(request.form.get('exclude_addr'))

    total = 0.0
    item_rows = []
    for i in range(len(descriptions)):
        desc = (descriptions[i] or '').strip()
        if not desc:
            continue
        qty = int(quantities[i]) if i < len(quantities) and quantities[i] else 0
        rate = float(rates[i]) if i < len(rates) and rates[i] else 0.0
        dc_val = ''
        if dc_numbers and i < len(dc_numbers) and dc_numbers[i]:
            dc_val = dc_numbers[i].strip()
        rounded = (i < len(rounded_flags) and rounded_flags[i] == "1")
        raw_total = qty * rate
        line_total = rounding_to_nearest_zero(raw_total) if rounded else raw_total
        total += line_total
        item_rows.append([desc, qty, rate, line_total, dc_val, rounded])

    # Create invoice
    new_invoice = invoice(
        customerId=selected_customer.id,
        createdAt=datetime.now(timezone.utc),
        totalAmount=(round(total, 2)),
        pdfPath="",  # set after inv_name built
        invoiceId="",  # temporary
        exclude_phone=exclude_phone,
        exclude_gst=exclude_gst,
        exclude_addr=exclude_addr
    )

    db.session.add(new_invoice)
    db.session.flush()
    # Add Alert - Not needed

    # Generate invoice Id + pdf path. Prefix is configurable via the
    # `bill_config.invoice_prefix` info.json key (or BG_INVOICE_PREFIX env);
    # falls back to the generic default.
    invoice_prefix = (
        APP_INFO.get("bill_config", {}).get("invoice_prefix")
        or DEFAULT_INVOICE_PREFIX
    ).strip("- ") or DEFAULT_INVOICE_PREFIX
    inv_name = f"{invoice_prefix}-{datetime.now().strftime('%d%m%y')}-{str(new_invoice.id).zfill(5)}"
    pdf_filename = f"{inv_name}.pdf"
    pdf_path = os.path.join("static/pdfs", pdf_filename)

    new_invoice.invoiceId = inv_name
    new_invoice.pdfPath = pdf_path
    db.session.flush()
    # add alerts - not needed as persistant on in place

    # Add line items
    for desc, qty, rate, line_total, dc_val, rounded in item_rows:
        matched_item = item.query.filter_by(name=desc).first()
        if matched_item:
            item_id = matched_item.id
        else:
            new_item = item(name=desc, unitPrice=rate, quantity=0, taxPercentage=0)
            db.session.add(new_item)
            db.session.flush()
            # add alert - not needed as persistent one in place
            item_id = new_item.id

        db.session.add(invoiceItem(
            invoiceId=new_invoice.id,
            itemId=item_id,
            quantity=qty,
            rate=rate,
            discount=0,
            taxPercentage=0,
            line_total=line_total,
            dcNo=(dc_val if dc_val else None),
            rounded=rounded,
        ))

    draft_id = request.form.get('draft_id', type=int)
    if draft_id:
        draft_record = (
            billDraft.query
            .filter(
                billDraft.id == draft_id,
                billDraft.customerId == selected_customer.id,
                billDraft.status == 'draft',
            )
            .first()
        )
        if draft_record:
            draft_record.status = 'converted'
            draft_record.convertedInvoiceId = new_invoice.id
            draft_record.updatedAt = datetime.now(timezone.utc)
    db.session.commit()

    # add alerts - Not needed, persistent one is in place

    # Did user include any DC values?
    # dc_present = any((x or '').strip() for x in (dc_numbers or []))

    # After successful creation, flash and redirect to locked preview page
    session['persistent_notice'] = f"Invoice {new_invoice.invoiceId} created successfully!"
    return redirect(url_for('view_bill_locked', invoicenumber=new_invoice.invoiceId, edit_bill='true'))


@app.route('/view_customers', methods=['GET', 'POST'])
def view_customers():
    if request.method == 'POST':
        phone = request.form.get('customer')
        sel = (customer.query
               .filter(customer.isDeleted == False, customer.phone == phone)
               .first_or_404())
        return _render_create_bill(customer=sel, inventory=item.query.all())

    query = (request.args.get('q') or '').strip().lower()
    q = (customer.query
         .filter(customer.isDeleted == False)
         .order_by(customer.createdAt.desc(), customer.id.desc()))
    customers = q.all()

    if query:
        customers = [
            c for c in customers
            if query in (c.company or '').lower()
               or query in (c.name or '').lower()
               or query in (c.phone or '')
        ]

    return render_template('view_customers.html', customers=customers)


@app.route('/view_bills')
def view_bills():
    """Render all bills with filtering, search, and sorting."""

    # ---- 1️⃣ Extract filters from query params ----
    query = (request.args.get('q') or '').lower()
    phone = request.args.get('phone')
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')

    # ---- 2️⃣ Base query with eager loading ----
    q = (
        invoice.query
        .options(joinedload(invoice.customer))
        .join(customer, invoice.customerId == customer.id)
        .filter(invoice.isDeleted == False, customer.isDeleted == False)
    )

    # ---- 3️⃣ Sorting ----
    sort_key = (request.args.get('sort') or 'date').lower()
    sort_dir = (request.args.get('dir') or 'desc').lower()

    def order(col):
        return col.desc() if sort_dir == 'desc' else col.asc()

    if sort_key == 'total':
        q = q.order_by(order(invoice.totalAmount))
    elif sort_key == 'invoice':
        q = q.order_by(order(invoice.invoiceId))
    elif sort_key == 'customer':
        q = q.order_by(order(customer.name))
    else:
        q = q.order_by(order(invoice.createdAt))

    # ---- 4️⃣ Optional date range filter ----
    try:
        if start_date and end_date:
            start_dt = datetime.strptime(start_date, '%Y-%m-%d')
            end_dt = datetime.strptime(end_date, '%Y-%m-%d') + timedelta(days=1)
            q = q.filter(invoice.createdAt >= start_dt, invoice.createdAt < end_dt)
    except Exception:
        pass

    # ---- 5️⃣ Execute main query ----
    invoices = q.all()

    # ---- 6️⃣ Transform for template ----
    bills = []
    for inv in invoices:
        cust = inv.customer
        bills.append({
            "invoice_no": inv.invoiceId,
            "date": inv.createdAt.strftime('%d-%b-%Y'),
            "customer_name": cust.name if cust else 'Unknown',
            "phone": cust.phone if cust else '',
            "total": f"{inv.totalAmount:,.2f}",
            "filename": f"{inv.invoiceId}.pdf",
            "customer_company": cust.company if cust else 'Unknown',
            "is_paid": bool(getattr(inv, 'payment', False))
        })

    # ---- 7️⃣ Apply search filters ----
    if phone:
        bills = [b for b in bills if b['phone'] == phone]
    elif query:
        bills = [
            b for b in bills
            if query in b['customer_name'].lower()
               or query in b.get('phone', '')
               or query in b['invoice_no'].lower()
               or query in (b.get('customer_company') or '').lower()
        ]

    # ---- 8️⃣ Render ----
    current_filters_url = request.full_path if request.query_string else request.path
    return render_template('view_bills.html', bills=bills, mark_paid_redirect=current_filters_url)


@app.route('/view-bill/<invoicenumber>')
def view_bill_locked(invoicenumber):
    # load invoice and related data
    current_invoice = invoice.query.filter_by(invoiceId=invoicenumber, isDeleted=False).first_or_404()
    cur_cust = db.session.get(customer, current_invoice.customerId)
    line_items = invoiceItem.query.filter_by(invoiceId=current_invoice.id).all()
    customer_bill_navigation = []
    for history_row in _get_customer_bill_history(getattr(cur_cust, 'id', None)):
        customer_bill_navigation.append({
            **history_row,
            'is_current': history_row['invoice_no'] == current_invoice.invoiceId,
        })

    current_customer = {
        "name": cur_cust.name,
        "company": cur_cust.company,
        "phone": "Excluded in the bill" if current_invoice.exclude_phone else cur_cust.phone,
        "gst": "Excluded in the bill" if current_invoice.exclude_gst else cur_cust.gst,
        "address": "Excluded in the bill" if current_invoice.exclude_addr else cur_cust.address,
        "email": cur_cust.email
    }

    # build row wise lists for the template
    descriptions, quantities, rates, dc_numbers = [], [], [], []
    line_totals, rounded_flags = [], []

    total = 0.0
    for li in line_items:
        itm = db.session.get(item, li.itemId)
        descriptions.append(itm.name if itm else 'Unknown')
        quantities.append(li.quantity)
        rates.append(li.rate)
        dc_numbers.append(li.dcNo or '')
        line_totals.append(li.line_total)
        rounded_flags.append('1' if getattr(li, 'rounded', False) else '0')
        total += li.line_total or 0

    # Determine whether to show DC column
    dcno = any((x or '').strip() for x in dc_numbers)

    edit_bill = request.args.get('edit_bill', '').lower() in ('yes', 'true', '1')
    back_two_pages = edit_bill

    invoice_date = current_invoice.createdAt
    invoice_paid = bool(getattr(current_invoice, 'payment', False))

    current_page_url = request.full_path if request.query_string else request.path
    return render_template(
        'view_bill_locked.html',
        customer=current_customer,
        descriptions=descriptions,
        quantities=quantities,
        rates=rates,
        dc_numbers=dc_numbers,
        line_totals=line_totals,
        rounded_flags=rounded_flags,
        dcno=dcno,
        total=round(total, 2),
        invoice_no=current_invoice.invoiceId,
        back_to_select_customer=False,
        back_to_url=None,
        customer_id=cur_cust.id,
        back_two_pages=back_two_pages,
        invoice_date=invoice_date,
        mark_paid_redirect=current_page_url,
        is_paid=invoice_paid,
        customer_bill_navigation=customer_bill_navigation,
    )


@app.route('/bill_preview_dues/<invoicenumber>')
def bill_preview_dues(invoicenumber):
    current_invoice = invoice.query.filter_by(invoiceId=invoicenumber, isDeleted=False).first_or_404()
    cur_cust = db.session.get(customer, current_invoice.customerId)
    due_candidates, summary_rows, summary_total = _build_due_summary_rows(current_invoice, include_current=True)

    current_customer = {
        "name": cur_cust.name,
        "company": cur_cust.company,
        "phone": cur_cust.phone,
        "gst": cur_cust.gst,
        "address": cur_cust.address,
        "email": cur_cust.email,
    }

    return render_template(
        'bill_preview_dues.html',
        invoice=current_invoice,
        customer=current_customer,
        due_candidates=due_candidates,
        current_bill_row=(summary_rows[0] if summary_rows else {
            'invoice_no': current_invoice.invoiceId,
            'created_at': current_invoice.createdAt,
            'date_label': current_invoice.createdAt.strftime('%d %b %Y') if current_invoice.createdAt else '',
            'amount': float(round(current_invoice.totalAmount or 0, 2)),
            'is_current': True,
        }),
        current_bill_is_paid=bool(getattr(current_invoice, 'payment', False)),
        mark_paid_redirect=request.full_path if request.query_string else request.path,
        preview_base_url=url_for('bill_preview', invoicenumber=current_invoice.invoiceId),
    )


def _build_bill_preview_context(current_invoice, *, include_due_summary=False, include_current_in_due_summary=False, selected_due_invoice_nos=None):
    cur_cust = db.session.get(customer, current_invoice.customerId)

    current_customer = {
        "name": cur_cust.name,
        "company": cur_cust.company,
        "phone": None if current_invoice.exclude_phone else cur_cust.phone,
        "gst": None if current_invoice.exclude_gst else cur_cust.gst,
        "address": None if current_invoice.exclude_addr else cur_cust.address,
        "email": cur_cust.email
    }
    items = invoiceItem.query.filter_by(invoiceId=current_invoice.id).all()

    item_data = []
    for current_item in items:
        item_name = db.session.get(item, current_item.itemId).name if current_item.itemId else "Unknown"
        entry = (
            item_name,
            "N/A",
            current_item.quantity,
            current_item.rate,
            current_item.discount,
            current_item.taxPercentage,
            current_item.line_total
        )
        item_data.append(entry)

    dc_numbers = [current_item.dcNo or '' for current_item in items]
    dcno = any(bool((x or '').strip()) for x in dc_numbers)

    config = layoutConfig.get_or_create()
    current_sizes = config.get_sizes()

    upi_id = APP_INFO["upi_info"]["upi_id"]
    company_name = APP_INFO["business"]["name"]
    upi_name = APP_INFO["upi_info"]["upi_name"]
    brand_watermark_path = _resolve_brand_watermark_path(APP_INFO.get("business"))
    brand_accent_color = _resolve_brand_accent_color(APP_INFO.get("business"))
    to_color = _resolve_to_color(APP_INFO.get("invoice_visual"))
    _, due_summary_rows, due_summary_total = _build_due_summary_rows(
        current_invoice,
        selected_due_invoice_nos=selected_due_invoice_nos,
        include_current=(include_due_summary and include_current_in_due_summary),
    )
    qr_total = due_summary_total if due_summary_rows else float(current_invoice.totalAmount or 0)

    api_url = f"{request.host_url.rstrip('/')}/api/generate_upi_qr"
    params = _build_upi_qr_params(
        upi_id=upi_id,
        amount=qr_total,
        payee_name=upi_name or company_name,
        currency=APP_INFO.get("upi_info", {}).get("currency"),
    )

    try:
        resp = requests.get(api_url, params=params, timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            qr_svg_base64 = data.get('qr_svg_base64')
            upi_url = data.get('upi_url')
        else:
            qr_svg_base64 = None
            upi_url = None
    except Exception as e:
        app.logger.warning("failed to fetch QR: %s", e)
        qr_svg_base64 = None
        upi_url = None

    return {
        'invoice': current_invoice,
        'customer': current_customer,
        'items': item_data,
        'dcno': dcno,
        'dc_numbers': dc_numbers,
        'total_in_words': amount_to_words(current_invoice.totalAmount),
        'sizes': current_sizes,
        'qr_svg_base64': qr_svg_base64,
        'upi_id': upi_id,
        'upi_name': upi_name,
        'company_name': company_name,
        'total': current_invoice.totalAmount,
        'app_info': APP_INFO,
        'to_color': to_color,
        'brand_watermark_path': brand_watermark_path,
        'brand_accent_color': brand_accent_color,
        'due_summary_rows': due_summary_rows,
        'due_summary_total': due_summary_total,
        'show_due_summary': bool(due_summary_rows),
        'qr_total': qr_total,
    }


def mark_bill_paid(invoice_no):
    invoice_obj = invoice.query.filter_by(invoiceId=invoice_no, isDeleted=False).first_or_404()
    customer_obj = customer.query.filter_by(id=invoice_obj.customerId, isDeleted=False).first()

    raw_next = request.form.get('next') or ''
    next_url = raw_next or url_for('view_bills')
    parsed_next = urlparse(next_url)
    if parsed_next.netloc and parsed_next.netloc != request.host:
        next_url = url_for('view_bills')

    if getattr(invoice_obj, 'payment', False):
        flash('Invoice already marked as paid.', 'info')
        return redirect(next_url)

    if not customer_obj:
        flash('Unable to record payment: customer details missing for this invoice.', 'danger')
        return redirect(next_url)

    amount_value = _get_invoice_outstanding_amount(invoice_obj)
    if amount_value <= 0:
        _sync_invoice_payment_flag(invoice_obj.invoiceId)
        db.session.commit()
        flash('Invoice already has no outstanding balance.', 'info')
        return redirect(next_url)

    source = (request.form.get('source') or 'view_bills').strip().lower()
    remarks_map = {
        'view_bill_locked': 'Marked as paid via bill detail page.',
        'view_bills': 'Marked as paid via bills list.',
        'bill_preview_dues': 'Marked as paid from Bill with Dues page.',
        'accounting_customer': 'Marked as paid from customer accounting page.',
        'accounting_statement': 'Marked as paid from company statement page.',
    }
    remarks = remarks_map.get(source, 'Marked as paid.')

    txn = accountingTransaction(
        customerId=customer_obj.id,
        amount=amount_value,
        txn_type='income',
        mode='bank',
        account='current',
        invoice_no=invoice_obj.invoiceId,
        remarks=remarks,
    )
    db.session.add(txn)
    db.session.flush()
    _sync_invoice_payment_flag(invoice_obj.invoiceId)
    try:
        db.session.commit()
        flash(f'Recorded payment of INR {amount_value:,.2f} for invoice {invoice_obj.invoiceId}.', 'success')
    except Exception as exc:
        db.session.rollback()
        flash(f'Unable to record payment: {exc}', 'danger')

    return redirect(next_url)


app.add_url_rule(
    '/bills/<invoice_no>/mark-paid',
    view_func=mark_bill_paid,
    endpoint='mark_bill_paid',
    methods=['POST']
)


# amounts to words
# --- Amount to words (Indian numbering: Crore, Lakh, Thousand) ---
ONES = [
    "", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine",
    "Ten", "Eleven", "Twelve", "Thirteen", "Fourteen", "Fifteen", "Sixteen",
    "Seventeen", "Eighteen", "Nineteen"
]
TENS = ["", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety"]


def _two_digits(n: int) -> str:
    if n == 0:
        return ""
    if n < 20:
        return ONES[n]
    return (TENS[n // 10] + (" " + ONES[n % 10] if (n % 10) != 0 else "")).strip()


def _three_digits(n: int) -> str:
    # 0..999
    if n >= 100:
        rem = n % 100
        s = ONES[n // 100] + " Hundred"
        if rem:
            s += " and " + _two_digits(rem)
        return s
    return _two_digits(n)


def rupees_to_words(num: int) -> str:
    if num == 0:
        return "Zero"
    parts = []
    crore = num // 10000000;
    num %= 10000000
    lakh = num // 100000;
    num %= 100000
    thousand = num // 1000;
    num %= 1000
    rest = num  # 0..999

    # Use _three_digits for groups that can be up to 999
    if crore:
        parts.append(_three_digits(int(crore)) + " Crore")
    if lakh:
        parts.append(_three_digits(int(lakh)) + " Lakh")
    if thousand:
        parts.append(_three_digits(int(thousand)) + " Thousand")
    if rest:
        parts.append(_three_digits(int(rest)))

    return " ".join(parts)


def amount_to_words(amount) -> str:
    try:
        amt = float(amount or 0)
    except Exception:
        amt = 0.0
    rupees = int(amt)
    paise = int(round((amt - rupees) * 100))
    words = rupees_to_words(rupees) + " Rupees"
    if paise:
        words += " and " + _two_digits(paise) + " Paise"
    return words + " Only"


@app.route('/bill_preview/<invoicenumber>')
def bill_preview(invoicenumber):
    current_invoice = invoice.query.filter_by(invoiceId=invoicenumber, isDeleted=False).first_or_404()
    if not current_invoice:
        return f"No invoice found for {invoicenumber}"

    include_due_summary = (request.args.get('with_dues') or '').strip().lower() in {'1', 'true', 'yes'}
    include_current_in_due_summary = (request.args.get('include_current') or '').strip().lower() in {'1', 'true', 'yes'}
    selected_due_invoice_nos = request.args.getlist('selected_due')
    context = _build_bill_preview_context(
        current_invoice,
        include_due_summary=include_due_summary,
        include_current_in_due_summary=include_current_in_due_summary,
        selected_due_invoice_nos=selected_due_invoice_nos,
    )
    return render_template('bill_preview.html', **context)


@app.route('/edit-bill/<invoicenumber>', methods=['GET', 'POST'])
def edit_bill(invoicenumber):
    # fetch invoice and related data
    current_invoice = invoice.query.filter_by(invoiceId=invoicenumber).first_or_404()
    current_customer = db.session.get(customer, current_invoice.customerId)
    line_items = invoiceItem.query.filter_by(invoiceId=current_invoice.id).all()

    # Build lists for template
    descriptions, quantities, rates, dc_numbers = [], [], [], []
    line_totals, rounded_flags = [], []
    total = 0.0
    for li in line_items:
        itm = db.session.get(item, li.itemId)
        descriptions.append(itm.name if itm else 'Unknown')
        quantities.append(li.quantity)
        rates.append(li.rate)
        dc_numbers.append(li.dcNo or '')
        line_totals.append(li.line_total or 0)
        rounded_flags.append('1' if getattr(li, 'rounded', False) else '0')
        total += li.line_total or 0

    dcno = any((x or '').strip() for x in dc_numbers)

    prev_invoice_no = current_invoice.invoiceId
    try:
        prev_created_at = current_invoice.createdAt.strftime('%Y-%m-%d %H:%M')
    except Exception:
        prev_created_at = str(current_invoice.createdAt)

    exclude_phone = current_invoice.exclude_phone
    exclude_gst = current_invoice.exclude_gst
    exclude_addr = current_invoice.exclude_addr

    # If POST: update invoice and redirect to view_bill_locked
    if request.method == 'POST':
        # Update customer-level metadata before saving invoice
        current_invoice.exclude_phone = request.form.get('exclude_phone') in ('on', 'true', '1')
        current_invoice.exclude_gst = request.form.get('exclude_gst') in ('on', 'true', '1')
        current_invoice.exclude_addr = request.form.get('exclude_addr') in ('on', 'true', '1')
        if not _safe_commit('update invoice metadata'):
            flash('Could not save invoice settings. Please try again.', 'danger')
            return redirect(url_for('edit_bill', invoicenumber=current_invoice.invoiceId))
        return redirect(url_for('view_bill_locked', invoicenumber=current_invoice.invoiceId, edit_bill='true'))

    # Render the same template as create_bill.html but pre-filled
    return _render_create_bill(
        customer=current_customer,
        inventory=item.query.all(),
        success=False,  # show filled rows
        descriptions=descriptions,
        quantities=quantities,
        rates=rates,
        dc_numbers=dc_numbers,
        dcno=dcno,
        total=round(total, 2),
        grand_total=round(total, 2),
        invoice_no=current_invoice.invoiceId,
        edit_mode=True,  # flag to distinguish editing vs new bill
        prev_invoice_no=prev_invoice_no,
        prev_created_at=prev_created_at,
        exclude_phone=exclude_phone,
        exclude_gst=exclude_gst,
        exclude_addr=exclude_addr,
        line_totals=line_totals,
        rounded_flags=rounded_flags,
        show_prefilled_rows=bool(descriptions),
    )


@app.route('/delete-bill/<invoicenumber>', methods=['POST'])
def delete_bill(invoicenumber):
    inv = invoice.query.filter_by(invoiceId=invoicenumber, isDeleted=False).first_or_404()
    inv.isDeleted = True
    inv.deletedAt = datetime.now(timezone.utc)
    if not _safe_commit('delete bill'):
        flash('Could not delete the bill. Please try again.', 'danger')
        return redirect(url_for('view_bills'))
    flash('Bill has been deleted.', 'danger')

    next_url = request.form.get('next') or ''
    try:
        host = urlparse(request.host_url).netloc
        parsed = urlparse(next_url)
        if next_url and (parsed.netloc == '' or parsed.netloc == host):
            return redirect(next_url)
    except Exception:
        pass
    return redirect(url_for('view_bills'))


@app.route('/update-bill/<invoicenumber>', methods=['POST'])
def update_bill(invoicenumber):
    # 1) Load the invoice being edited
    current_invoice = invoice.query.filter_by(invoiceId=invoicenumber, isDeleted=False).first_or_404()
    current_customer = db.session.get(customer, current_invoice.customerId)

    submitted_token = request.form.get('form_token')
    if not _validate_bill_token(submitted_token):
        flash('The bill form expired or was already submitted. Reopen the bill and make your changes again.', 'warning')
        return redirect(url_for('edit_bill', invoicenumber=invoicenumber))

    # 2) Read form inputs
    descriptions = request.form.getlist('description[]')
    quantities = request.form.getlist('quantity[]')
    rates = request.form.getlist('rate[]')
    dc_numbers = request.form.getlist('dc_no[]')  # may be empty if toggle off
    rounded_flags = request.form.getlist('rounded[]')

    # 3) Normalize rows + recompute totals
    rows = []
    total = 0.0
    for i in range(len(descriptions)):
        desc = (descriptions[i] or '').strip()
        if not desc:
            continue  # skip empty rows

        qty = int(quantities[i]) if i < len(quantities) and quantities[i] else 0
        rate = float(rates[i]) if i < len(rates) and rates[i] else 0.0
        dc = (dc_numbers[i].strip() if i < len(dc_numbers) and dc_numbers[i] else None)
        rounded = (i < len(rounded_flags) and rounded_flags[i] == "1")
        raw_total = qty * rate
        line_total = rounding_to_nearest_zero(raw_total) if rounded else raw_total
        total += line_total
        rows.append((desc, qty, rate, dc, line_total, rounded))

    # 4) Replace all existing line items with the new set using ORM deletes so sync events fire
    existing_items = invoiceItem.query.filter_by(invoiceId=current_invoice.id).all()
    for existing_item in existing_items:
        db.session.delete(existing_item)
    db.session.flush()

    for desc, qty, rate, dc, line_total, rounded in rows:
        # Reuse existing item by name, or create a placeholder item if not found
        matched_item = item.query.filter_by(name=desc).first()
        if matched_item:
            item_id = matched_item.id
        else:
            new_item = item(name=desc, unitPrice=rate, quantity=0, taxPercentage=0)
            db.session.add(new_item)
            db.session.flush()
            item_id = new_item.id

        db.session.add(invoiceItem(
            invoiceId=current_invoice.id,
            itemId=item_id,
            quantity=qty,
            rate=rate,
            discount=0,
            taxPercentage=0,
            line_total=line_total,
            dcNo=dc if dc else None,
            rounded=rounded,
        ))

    # 5) Update invoice total (and updatedAt if you have it)
    current_invoice.totalAmount = (round(total, 2))

    # 5.5) Update customer-level metadata before saving invoice
    current_invoice.exclude_phone = request.form.get('exclude_phone') in ('on', 'true', '1')
    current_invoice.exclude_gst = request.form.get('exclude_gst') in ('on', 'true', '1')
    current_invoice.exclude_addr = request.form.get('exclude_addr') in ('on', 'true', '1')

    db.session.commit()
    # add alert - not needed persistent one in place

    # 6) Redirect to locked preview after update
    session['persistent_notice'] = f"Old invoice {current_invoice.invoiceId} updated successfully!"

    return redirect(url_for('view_bill_locked', invoicenumber=current_invoice.invoiceId, edit_bill='true', new_bill='true'))


@app.route('/bill_preview/latest')
def latest_bill_preview():
    current_invoice = (
        invoice.query
        .filter(invoice.isDeleted == False)
        .order_by(invoice.id.desc())
        .first()
    )
    if not current_invoice:
        return "No invoice found"

    context = _build_bill_preview_context(current_invoice)
    return render_template('bill_preview.html', **context)


app.jinja_env.globals.update(zip=zip)


@app.context_processor
def _inject_branding_globals():
    """Make app-wide branding values available in every template.

    Templates can now reference ``APP_NAME`` and ``APP_INFO`` directly
    without each route having to pass them in.
    """
    return {
        "APP_NAME": APP_NAME,
        "APP_INFO": APP_INFO,
    }


@app.route('/statements', methods=['GET'])
def statements():
    start_date, end_date = _resolve_legacy_statement_dates()
    phone = (request.args.get('phone') or '').strip()
    if phone:
        selected_customer = _find_customer_by_exact_phone(phone)
        if selected_customer:
            return redirect(url_for(
                'accounting_customer_simple_statement',
                customer_id=selected_customer.id,
                start=start_date.isoformat(),
                end=end_date.isoformat(),
            ))

    params = {
        'start': start_date.isoformat(),
        'end': end_date.isoformat(),
        'mode': 'simple',
    }
    if (request.args.get('export') or '').lower() == 'pdf':
        params['export'] = 'pdf'
    return redirect(url_for('accounting_statement', **params))


@app.route('/statements_company', methods=['GET', 'POST'])
def statements_company():
    query = (request.args.get('query') or '').strip()
    phone = (request.args.get('phone') or '').strip()
    customer_token = phone or query
    if phone:
        selected_customer = _find_customer_by_exact_phone(phone)
    else:
        selected_customer = _resolve_statement_customer_token(customer_token) if customer_token else None

    params = {}
    start = request.args.get('start')
    end = request.args.get('end')
    if start:
        params['start'] = start
    if end:
        params['end'] = end

    fmt = (request.args.get('format') or request.args.get('fmt') or 'html').strip().lower().replace('-', '_')
    if fmt == 'simple_pdf' and selected_customer:
        return redirect(url_for(
            'accounting_customer_simple_statement',
            customer_id=selected_customer.id,
            **params,
        ))
    if selected_customer:
        return redirect(url_for('accounting_customer_detail', customer_id=selected_customer.id, **params))
    if fmt == 'pdf':
        params['mode'] = 'accounting'
        params['export'] = 'pdf'
    elif fmt == 'simple_pdf':
        params['mode'] = 'simple'
        params['export'] = 'pdf'
    else:
        params['mode'] = 'simple'
    return redirect(url_for('accounting_statement', **params))


@app.route('/accounting/statement', methods=['GET'])
def accounting_statement():
    """
    Company-wide statement with simple and accounting modes.
    """
    export = (request.args.get('export') or 'html').lower()
    statement_mode = (request.args.get('mode') or 'simple').strip().lower()
    if statement_mode not in {'simple', 'accounting'}:
        statement_mode = 'simple'

    today = datetime.now(timezone.utc).date()
    min_start = get_default_statement_start().date()

    default_start = min_start

    start_date = _parse_date(request.args.get('start')) or default_start
    end_date = _parse_date(request.args.get('end')) or today
    if start_date < min_start:
        start_date = min_start
    if end_date < start_date:
        end_date = start_date

    start_dt = datetime.combine(start_date, datetime.min.time()).replace(tzinfo=timezone.utc)
    end_dt = datetime.combine(end_date, datetime.max.time()).replace(tzinfo=timezone.utc)

    context = _build_accounting_statement_context(
        start_dt,
        end_dt,
        start_date,
        end_date,
        txn_type_filter='all',
        customer_query='',
    )

    template_payload = dict(context, APP_INFO=APP_INFO)
    template_payload["statement_mode"] = statement_mode
    template_payload["mark_paid_redirect"] = request.full_path[:-1] if request.full_path.endswith('?') else request.full_path
    template_payload["pdf_title"] = _build_export_pdf_title(
        APP_INFO.get('business', {}).get('name') or 'company_statement',
        kind='accounting_statement' if statement_mode == 'accounting' else 'company_statement',
    )

    if export == 'pdf':
        if statement_mode == 'simple':
            return _render_company_simple_statement_pdf(start_date, end_date)
        return render_template('print_accounting_statement.html', **template_payload)

    return render_template('accounting_statement.html', **template_payload)


@app.route('/statements/accounting', methods=['GET'])
def accounting_statement_legacy():
    params = {}
    for key in ('start', 'end'):
        value = (request.args.get(key) or '').strip()
        if value:
            params[key] = value

    customer_token = (request.args.get('customer') or '').strip()
    export = (request.args.get('export') or '').strip().lower()
    selected_customer = _resolve_statement_customer_token(customer_token) if customer_token else None

    if selected_customer:
        if export == 'pdf':
            return redirect(url_for(
                'accounting_customer_statement',
                customer_id=selected_customer.id,
                **params,
            ))
        return redirect(url_for('accounting_customer_detail', customer_id=selected_customer.id, **params))

    params['mode'] = 'accounting'
    if export == 'pdf':
        params['export'] = 'pdf'
    return redirect(url_for('accounting_statement', **params))


@app.route('/qr_code', methods=['GET', 'POST'])
def qr_code():
    upi_variants = _get_upi_variants()
    default_variant = upi_variants[0] if upi_variants else {
        'key': 'primary',
        'upi_id': APP_INFO.get('upi_info', {}).get('upi_id') or APP_INFO.get('business', {}).get('upi_id', ''),
        'upi_name': APP_INFO.get('upi_info', {}).get('upi_name') or APP_INFO.get('business', {}).get('upi_name', ''),
    }

    return render_template('QR_code.html',
                           upi_id=default_variant.get('upi_id', ''),
                           upi_name=default_variant.get('upi_name', ''),
                           selected_variant=default_variant.get('key', 'primary'),
                           upi_variants=upi_variants,
                           qr_image=False)


@app.route('/generate_qr', methods=['GET', 'POST'])
def generate_qr():
    source = request.form if request.method == 'POST' else request.args
    amount = source.get('amount')

    upi_info_defaults = APP_INFO.get('upi_info', {}) if isinstance(APP_INFO.get('upi_info'), dict) else {}
    business_defaults = APP_INFO.get('business', {}) if isinstance(APP_INFO.get('business'), dict) else {}

    selected_variant = _find_upi_variant(source.get('upi_variant'))
    if selected_variant:
        upi_id = selected_variant.get('upi_id') or upi_info_defaults.get('upi_id') or business_defaults.get('upi_id')
        upi_name = selected_variant.get('upi_name') or upi_info_defaults.get('upi_name') or business_defaults.get('upi_name') or business_defaults.get('owner')
    else:
        upi_id = source.get('upi_id') or upi_info_defaults.get('upi_id') or business_defaults.get('upi_id')
        upi_name = source.get('upi_name') or upi_info_defaults.get('upi_name') or business_defaults.get('upi_name') or business_defaults.get('owner')

    company_name = business_defaults.get('name') or APP_INFO['business']['name']

    api_url = f"{request.host_url.rstrip('/')}/api/generate_upi_qr"
    params = _build_upi_qr_params(
        upi_id=upi_id,
        amount=amount,
        payee_name=upi_name or company_name,
        currency=APP_INFO.get("upi_info", {}).get("currency"),
    )

    try:
        resp = requests.get(api_url, params=params, timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            qr_svg_base64 = data.get('qr_svg_base64')
            upi_url = data.get('upi_url')
        else:
            qr_svg_base64 = None
            upi_url = None
    except Exception as e:
        app.logger.warning("failed to fetch QR: %s", e)
        qr_svg_base64 = None
        upi_url = None

    qr_details = {
        'upi_id': upi_id,
        'company_name': company_name,
        'amount': amount
    }

    return render_template(
        'qr_display.html',
        upi_id=upi_id,
        qr_image=True,
        amount=amount,
        upi_name=upi_name,
        qr_code=qr_svg_base64,
        upi_url=upi_url,
        business_name=APP_INFO['business']['name'],
        amount_to_words=amount_to_words(amount)
    )


def load_supabase_config():
    try:
        url = APP_INFO['supabase']['url']
        key = APP_INFO['supabase']['key']
        last_updated = APP_INFO['supabase']['last_uploaded']
        return url, key, last_updated
    except Exception as e:
        print(f"Could not load supabase config: {e}")
        return None, None, None


def _update_supabase_last_uploaded(timestamp: str) -> None:
    info_path = ensure_info_json()
    try:
        with open(info_path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except Exception:
        payload = {"data": {}}

    payload.setdefault("data", {})
    supabase_info = dict(payload["data"].get("supabase", {}))
    supabase_info["last_uploaded"] = timestamp
    payload["data"]["supabase"] = supabase_info

    try:
        with open(info_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
    except Exception as exc:
        print(f"[warn] Failed to update Supabase sync metadata: {exc}")
        return

    refresh_info_json()


def _update_supabase_last_incremental(timestamp: str) -> None:
    info_path = ensure_info_json()
    try:
        with open(info_path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except Exception:
        payload = {"data": {}}

    payload.setdefault("data", {})
    supabase_info = dict(payload["data"].get("supabase", {}))
    supabase_info["last_incremental_uploaded"] = timestamp
    payload["data"]["supabase"] = supabase_info

    try:
        with open(info_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
    except Exception as exc:
        print(f"[warn] Failed to update Supabase incremental metadata: {exc}")
        return

    refresh_info_json()


def _supabase_credentials_ready() -> bool:
    supabase_meta = APP_INFO.get('supabase', {})
    url = (supabase_meta.get('url') or '').strip()
    key = (supabase_meta.get('key') or '').strip()
    return bool(url and key)


def _instant_uploads_enabled() -> bool:
    # Disable automatic Supabase uploads; only manual triggers should sync.
    return False


def _should_flash_sync_error() -> bool:
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return False
    accepts_html = request.accept_mimetypes['text/html']
    accepts_json = request.accept_mimetypes['application/json']
    return accepts_html >= accepts_json


def _sync_pending_activity_logs() -> tuple[str, Optional[str]]:
    if not _instant_uploads_enabled():
        return "skipped", None

    if not activity_logs_pending() or not _supabase_credentials_ready():
        return "skipped", None

    url, key, _ = load_supabase_config()
    if not url or not key:
        return "skipped", None

    try:
        result = upload_to_supabase(url, key, include_analytics=False)
    except Exception as exc:
        return "error", str(exc)

    db_result = result["db"]
    if db_result.failed == 0:
        clear_activity_pending_flag()
        if db_result.uploaded:
            timestamp = datetime.now(timezone.utc).isoformat()
            _update_supabase_last_incremental(timestamp)
        return "success", None

    message = "Unable to sync recent changes with Supabase."
    if db_result.failure_details:
        detail_payload = db_result.failure_details[0].get("details")
        if isinstance(detail_payload, dict):
            message = detail_payload.get("message") or detail_payload.get("error") or message
        elif detail_payload:
            message = str(detail_payload)
    return "error", message


def _resolve_external_backup_dir() -> Optional[Path]:
    """Return configured external backup directory, or None if unset."""
    location = (APP_INFO.get('file_location') or '').strip()
    if not location:
        return None
    try:
        return Path(location).expanduser()
    except Exception as exc:
        print(f"[warn] Invalid file_location '{location}': {exc}")
        return None


def _prune_backup_dir(directory: Path) -> None:
    """Keep only the newest BACKUP_RETENTION backup files in directory."""
    try:
        backups = sorted(
            directory.glob(f"{DATABASE_FILENAME}.*.bak"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except Exception as exc:
        print(f"[warn] Failed to enumerate external backups in {directory}: {exc}")
        return

    for old_backup in backups[BACKUP_RETENTION:]:
        try:
            old_backup.unlink()
        except Exception as exc:
            print(f"[warn] Failed to prune external backup {old_backup}: {exc}")


def _copy_backup_to_external(backup_source: Optional[Path] = None) -> Optional[Path]:
    """Copy the DB (or provided backup file) to configured external location."""
    destination_dir = _resolve_external_backup_dir()
    if not destination_dir:
        return None

    try:
        destination_dir.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        print(f"[warn] Unable to prepare external backup directory {destination_dir}: {exc}")
        return None

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    source_path = backup_source if backup_source and backup_source.exists() else DB_PATH
    if backup_source and backup_source.exists() and backup_source.suffix == '.bak':
        target_name = backup_source.name
    else:
        target_name = f"{DATABASE_FILENAME}.{timestamp}.bak"

    target_path = destination_dir / target_name

    try:
        shutil.copy2(source_path, target_path)
    except Exception as exc:
        print(f"[warn] Failed to copy backup to {target_path}: {exc}")
        return None

    _prune_backup_dir(destination_dir)
    return target_path


def _create_db_backup() -> Path | None:
    backup_dir = DATA_DIR / BACKUP_DIRNAME
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup_path = backup_dir / f"{DATABASE_FILENAME}.{timestamp}.bak"
    try:
        shutil.copy2(DB_PATH, backup_path)
    except Exception as exc:
        print(f"[warn] Failed to create DB backup: {exc}")
        return None

    try:
        backups = sorted(backup_dir.glob(f"{DATABASE_FILENAME}.*.bak"), key=lambda p: p.stat().st_mtime, reverse=True)
        for old_backup in backups[BACKUP_RETENTION:]:
            try:
                old_backup.unlink()
            except Exception as cleanup_exc:
                print(f"[warn] Failed to prune old backup {old_backup}: {cleanup_exc}")
    except Exception as exc:
        print(f"[warn] Failed to prune backups: {exc}")

    external_path = _copy_backup_to_external(backup_path)
    if external_path:
        print(f"[info] External backup saved to {external_path}")

    return backup_path


def _format_sync_timestamp(raw_value):
    if not raw_value:
        return "Never"
    try:
        parsed = parser.parse(str(raw_value))
    except Exception:
        return str(raw_value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    local_dt = parsed.astimezone(tz.tzlocal())
    date_part = local_dt.strftime("%d %b %Y")
    time_part = local_dt.strftime("%I:%M %p").lstrip('0')
    return f"{date_part} · {time_part}"


def _latest_backup_path() -> Path | None:
    backup_dir = DATA_DIR / BACKUP_DIRNAME
    if not backup_dir.exists():
        return None
    try:
        backups = sorted(
            backup_dir.glob(f"{DATABASE_FILENAME}.*.bak"),
            key=lambda p: p.stat().st_mtime,
            reverse=True
        )
    except Exception as exc:
        print(f"[warn] Failed to inspect backups: {exc}")
        return None
    return backups[0] if backups else None


def _ensure_recent_backup_on_shutdown() -> None:
    try:
        last_backup = _latest_backup_path()
        if last_backup is None:
            created = _create_db_backup()
            if created:
                print(f"[info] Shutdown backup created (no prior backups): {created}")
            return

        now_utc = datetime.now(timezone.utc)
        last_backup_time = datetime.fromtimestamp(last_backup.stat().st_mtime, tz=timezone.utc)
        age = now_utc - last_backup_time
        if age > timedelta(days=BACKUP_MAX_AGE_DAYS):
            created = _create_db_backup()
            if created:
                print(f"[info] Shutdown backup created (stale backup): {created}")
    except Exception as exc:
        print(f"[warn] Backup-on-shutdown failed: {exc}")


atexit.register(_ensure_recent_backup_on_shutdown)


def _check_supabase_connectivity(url: str, timeout: float = 5.0) -> tuple[bool, str]:
    # Refuse anything that isn't an https Supabase project URL — this
    # makes the user-supplied URL non-useful as an SSRF gadget against
    # internal services.
    parsed = urlparse(url or "")
    if parsed.scheme != "https":
        return False, "Supabase URL must start with https://"
    host = (parsed.hostname or "").lower()
    if not host or "." not in host:
        return False, "Supabase URL is missing a valid hostname"
    # Block obvious internal targets. Allow public *.supabase.co/in/io
    # plus any custom domain that is not localhost / RFC1918 literal.
    if host in ("localhost", "127.0.0.1", "0.0.0.0", "::1"):
        return False, "Supabase URL must point to a public host"
    if host.startswith(("10.", "127.", "169.254.", "192.168.")) or host.startswith("172."):
        # 172.16.0.0/12 — coarse check; fine for a friendly warning.
        return False, "Supabase URL must point to a public host"

    health_url = f"{url.rstrip('/')}/auth/v1/health"
    try:
        response = requests.get(health_url, timeout=timeout)
    except requests.RequestException as exc:
        return False, str(exc)

    if response.status_code >= 500:
        return False, f"Supabase health check failed with status {response.status_code}"

    return True, ""


@app.post('/supabase_sync_all')
def supabase_sync_all():
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'

    def _respond(status: str, message: str, category: str = 'info', code: int = 200, **extra):
        if is_ajax:
            payload = {"status": status, "message": message}
            payload.update(extra)
            return jsonify(payload), code
        flash(message, category)
        return redirect(url_for('more'))

    url, key, _ = load_supabase_config()
    if not url or not key:
        return _respond('error', 'Supabase credentials are missing. Please update Account Settings.', 'danger', 400)

    reachable, error_message = _check_supabase_connectivity(url)
    if not reachable:
        return _respond('error', 'Cloud sync unavailable right now — check your internet connection and try again.', 'warning', 503, details=error_message)

    backup_path = _create_db_backup()

    if backup_path:
        print(f"[info] Created database backup at {backup_path}")

    try:
        result = upload_full_database(url, key)
    except SupabaseUploadError as exc:
        skipped = ', '.join(exc.skipped_tables) if exc.skipped_tables else 'none'
        detail = f" Reason: {exc.detail}" if exc.detail else ''
        message = (
            f"Supabase rejected {exc.failed_count} records in '{exc.failed_table}'. "
            f"Upload stopped before tables: {skipped}.{detail}"
        )
        return _respond('error', message, 'danger', 500, details=message)
    except Exception as exc:
        return _respond('error', f'Failed to sync with Supabase: {exc}', 'danger', 500)

    timestamp = datetime.now(timezone.utc).isoformat()
    _update_supabase_last_uploaded(timestamp)

    success_message = f"Uploaded {result.uploaded} records to Supabase ({result.failed} failed)."
    return _respond('success', success_message, 'success', 200, uploaded=result.uploaded, failed=result.failed)


@app.post('/backup/local_copy')
def create_local_backup_copy():
    refresh_info_json()
    destination = _resolve_external_backup_dir()
    if not destination:
        flash('Set a backup folder in Account Settings before creating local copies.', 'warning')
        return redirect(url_for('more'))

    backup_path = _copy_backup_to_external()
    if backup_path:
        flash(f'Backup copied to {backup_path}', 'success')
    else:
        flash('Unable to copy database to the configured folder. Check permissions and path.', 'danger')
    return redirect(url_for('more'))


@app.post('/backup/snapshot')
def create_backup_snapshot():
    backup_path = _create_db_backup()
    if backup_path:
        flash(f'Backup snapshot created at {backup_path}', 'success')
    else:
        flash('Unable to create backup snapshot. Check disk space and permissions.', 'danger')
    return redirect(url_for('more'))


@app.post('/supabase_sync_incremental')
def supabase_sync_incremental():
    url, key, _ = load_supabase_config()
    if not url or not key:
        return jsonify({
            "status": "error",
            "message": "Supabase credentials are missing. Please update Account Settings."
        }), 400

    reachable, error_message = _check_supabase_connectivity(url)
    if not reachable:
        return jsonify({
            "status": "error",
            "message": "Looks like the internet is down. Please reconnect and try again."
        }), 503

    try:
        result = upload_to_supabase(url, key)
    except Exception as exc:
        return jsonify({
            "status": "error",
            "message": f"Failed to upload incremental data: {exc}"
        }), 500

    db_result = result["db"]
    analytics_result = result["analytics"]
    archived = result["archived"]

    payload = {
        "status": "ok",
        "db_uploaded": db_result.uploaded,
        "db_failed": db_result.failed,
        "analytics_uploaded": analytics_result.uploaded,
        "analytics_failed": analytics_result.failed,
        "archived": archived,
    }

    if db_result.failed == 0 and analytics_result.failed == 0:
        timestamp = datetime.now(timezone.utc).isoformat()
        _update_supabase_last_incremental(timestamp)
        payload["message"] = (
            f"Uploaded {db_result.uploaded} changes"
            f" and {analytics_result.uploaded} analytics events."
        )
        payload["last_uploaded"] = _format_sync_timestamp(timestamp)
        clear_activity_pending_flag()
    else:
        payload["status"] = "partial"
        payload["message"] = (
            "Incremental sync finished with errors. "
            "Check logs/failed for details."
        )
        supabase_meta = APP_INFO.get('supabase', {})
        previous = supabase_meta.get('last_incremental_uploaded') or supabase_meta.get('last_uploaded')
        payload["last_uploaded"] = _format_sync_timestamp(previous)
        if db_result.failed == 0:
            clear_activity_pending_flag()

    return jsonify(payload)


@app.after_request
def auto_sync_after_request(response: Response):
    status, message = _sync_pending_activity_logs()
    if status == "success":
        response.headers["X-Cloud-Sync"] = "ok"
    elif status == "error":
        response.headers["X-Cloud-Sync"] = "error"
        if message:
            response.headers["X-Cloud-Sync-Message"] = message
        if message and _should_flash_sync_error():
            flash(f"Cloud sync failed: {message}", "danger")
    return response


if __name__ == '__main__':
    # Default to loopback so the dev server is not reachable from the LAN.
    # Set BG_BIND_HOST=0.0.0.0 only if you understand the implications
    # (no auth, no CSRF on most routes — see SECURITY.md).
    host = os.getenv("BG_BIND_HOST", "127.0.0.1")
    port = APP_PORT

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    try:
        sock.bind((host, port))
        sock.close()
    except OSError as exc:
        print(f"Port {port} is already in use. Close the existing server and try again.")
        raise SystemExit(1) from exc

    print(f"Starting WSGI server on http://{host}:{port}")
    serve(app, host=host, port=port)
