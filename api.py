from flask import Blueprint, jsonify
from db.models import invoice, invoiceItem, item
import io, base64
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from urllib.parse import urlencode, quote_plus
from flask import request
import segno

api_bp = Blueprint('api_bp', __name__)

@api_bp.route('/api/bill_items/<invoicenumber>')
def api_bill_items(invoicenumber):
    """
    Return JSON list of all items in an invoice.
    """
    inv = invoice.query.filter_by(invoiceId=invoicenumber, isDeleted=False).first()
    if not inv:
        return jsonify({"error": "Invoice Not Found"}), 404

    items = (
        invoiceItem.query
        .filter_by(invoiceId=inv.id)
        .join(item, invoiceItem.itemId == item.id)
        .add_columns(
            item.name.label("item_name"),
            invoiceItem.quantity,
            invoiceItem.rate,
            invoiceItem.taxPercentage,
            invoiceItem.line_total
        )
        .all()
    )

    rows = []
    for i in items:
        rows.append({
            "name": i.item_name,
            "quantity": i.quantity,
            "rate": round(i.rate or 0, 2),
            "tax": round(i.taxPercentage or 0, 2),
            "amount": round(i.line_total or 0, 2)
        })

    return jsonify({
        "invoice_no": inv.invoiceId,
        "customer": inv.customer.name if inv.customer else "Unknown",
        "items": rows,
        "total": round(inv.totalAmount or 0, 2)
    })


@api_bp.route('/api/generate_upi_qr')
def generate_upi_qr():
    """Generate QR code for the given amounta and upi_id.
    Default currency: INR, can be explicitly provided.
    UPIs will be generated as scalable UPI QR code as base64 SVG.
    Example: /api/generate_upi_qr?upi_id=acme@upi&am=500&pn=Acme&cu=INR"""

    def _format_amount(raw):
        if raw is None or raw == '':
            return None
        try:
            value = Decimal(str(raw)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        except (InvalidOperation, ValueError):
            return None
        if value <= 0:
            return None
        return format(value, 'f')

    # Accept both canonical and legacy param names for backwards compatibility
    upi_id = (request.args.get('upi_id') or request.args.get('pa') or '').strip()
    raw_amount = request.args.get('am') or request.args.get('amount')
    amount = _format_amount(raw_amount)
    name = (request.args.get('pn') or request.args.get('name') or request.args.get('company_name') or '').strip()
    note = (request.args.get('tn') or request.args.get('note') or '').strip()

    cur = (request.args.get('cu') or request.args.get('cur') or 'INR').strip().upper()

    if not upi_id:
        return jsonify({"Error": "No UPI ID provided"}), 400

    upi_params = {
        "pa": upi_id,
        "cu": cur or "INR",
    }

    if name:
        upi_params["pn"] = name
    if amount:
        upi_params["am"] = amount
    if note:
        upi_params["tn"] = note

    upi_url = f"upi://pay?{urlencode(upi_params, quote_via=quote_plus)}"

    qr = segno.make(upi_url, micro=False)

    buffer = io.BytesIO()
    qr.save(buffer, kind="svg", scale=5)

    svg_data = buffer.getvalue().decode("utf-8")

    svg_base64 = base64.b64encode(svg_data.encode("utf-8")).decode("utf-8")

    return jsonify({
        "upi_url": upi_url,
        "qr_svg_base64": svg_base64,
        "format": "svg"
    })
