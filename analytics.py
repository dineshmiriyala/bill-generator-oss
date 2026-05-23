from sqlalchemy import func
from db.models import *

def get_sales_trends(granularity="day"):
    """
    Returns sales totals aggregated by time period.
    granularity: "day", "month", or "year"
    Output: (labels, totals)
    """
    if granularity == "all_time":
        total = (
            db.session.query(func.coalesce(func.sum(invoice.totalAmount), 0))
            .filter(invoice.isDeleted == False)
            .filter(invoice.totalAmount != None)
            .filter(invoice.totalAmount > 0)
            .scalar()
        )
        return ["All Time"], [float(total or 0)]
    if granularity == "weekday":
        period = func.strftime("%w", invoice.createdAt)  # 0=Sunday ... 6=Saturday
    elif granularity == "day":
        period = func.strftime("%Y-%m-%d", invoice.createdAt)
    elif granularity == "year":
        period = func.strftime("%Y", invoice.createdAt)
    else:  # default month
        period = func.strftime("%Y-%m", invoice.createdAt)

    q = (
        db.session.query(
            period.label("period"),
            func.coalesce(func.sum(invoice.totalAmount), 0).label("total")
        )
        .filter(invoice.isDeleted == False)
        .filter(invoice.totalAmount != None)
        .filter(invoice.totalAmount > 0)
        .group_by(period)
        .order_by(period)
    )
    labels, totals = [], []
    for row in q:
        if granularity == "day":
            labels.append(str(row.period))  # YYYY-MM-DD
        elif granularity == "year":
            labels.append(str(row.period))  # YYYY
        elif granularity == "weekday":
            # Map 0-6 to weekday names (Sunday=0 in SQLite)
            day_map = {
                "0": "Sunday",
                "1": "Monday",
                "2": "Tuesday",
                "3": "Wednesday",
                "4": "Thursday",
                "5": "Friday",
                "6": "Saturday"
            }
            labels.append(day_map.get(str(row.period), str(row.period)))
        else:
            labels.append(str(row.period))  # YYYY-MM
        totals.append(float(row.total or 0))
    if granularity == "weekday":
        order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        ordered_labels, ordered_totals = [], []
        for d in order:
            if d in labels:
                idx = labels.index(d)
                ordered_labels.append(labels[idx])
                ordered_totals.append(totals[idx])
        return ordered_labels, ordered_totals
    return labels, totals

def get_top_customers(limit=5):
    """
    Returns top customers by total invoice revenue.
    Output: (customer_names, revenues)
    """
    q = (
        db.session.query(
            customer.company,
            func.sum(invoice.totalAmount).label("revenue")
        )
        .join(invoice, customer.id == invoice.customerId)
        .filter(invoice.isDeleted == False)
        .group_by(customer.company)
        .order_by(func.sum(invoice.totalAmount).desc())
        .limit(limit)
    )
    names, revenues = [], []
    for row in q:
        names.append(row.company)
        revenues.append(float(row.revenue))
    return names, revenues

def get_day_wise_billing():
    """
    Returns daily billing details (number of invoices and total revenue).
    Output: (labels, invoice_counts, totals)
    """
    period = func.strftime("%Y-%m-%d", invoice.createdAt)

    q = (
        db.session.query(
            period.label("period"),
            func.count(invoice.id).label("invoice_count"),
            func.coalesce(func.sum(invoice.totalAmount), 0).label("total")
        )
        .filter(invoice.isDeleted == False)
        .filter(invoice.totalAmount != None)
        .filter(invoice.totalAmount > 0)
        .group_by(period)
        .order_by(period)
    )

    labels, counts, totals = [], [], []
    for row in q:
        labels.append(str(row.period))
        counts.append(int(row.invoice_count))
        totals.append(float(row.total or 0))
    return labels, counts, totals


def get_customer_retention():
    """
    Returns one-time vs repeat customers.
    Output: (one_time, repeat)
    """
    subq = (
        db.session.query(
            invoice.customerId,
            func.count(invoice.id).label("inv_count")
        )
        .filter(invoice.isDeleted == False)
        .group_by(invoice.customerId)
        .subquery()
    )

    one_time = db.session.query(func.count()).filter(subq.c.inv_count == 1).scalar()
    repeat = db.session.query(func.count()).filter(subq.c.inv_count > 1).scalar()
    return one_time, repeat