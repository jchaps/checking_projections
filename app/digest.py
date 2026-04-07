import logging
import smtplib
from datetime import date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from app import db
from app.projections import build_projection, find_low_points

log = logging.getLogger(__name__)


def build_and_send_digest(config, conn, recurring_config):
    """Build and send the weekly email digest."""
    detail_days = config["digest"]["projection_days_detail"]
    lowpoint_days = config["digest"]["projection_days_lowpoint"]
    threshold = config["thresholds"]["low_balance_warning"]

    projection = build_projection(conn, config, recurring_config, days=lowpoint_days)
    cc_summary = build_cc_summary(conn, config)

    html = render_digest(projection, cc_summary, detail_days, lowpoint_days, threshold)
    send_email(html, config)


def build_cc_summary(conn, config):
    """Build credit card summary data, sorted by payment day (earliest first)."""
    cards = []
    for card in config["accounts"]["credit_cards"]:
        liability = db.get_liability(conn, card["name"])
        balance_row = conn.execute(
            "SELECT current_balance FROM account_balances WHERE account_id = ?",
            (card["name"],)
        ).fetchone()

        current_bal = balance_row["current_balance"] if balance_row else None
        stmt_bal = liability["last_statement_balance"] if liability else None
        min_pay = liability["minimum_payment"] if liability else None

        # Determine payment amount using same logic as projections
        strategy = card["payment_strategy"]
        if strategy == "statement_balance":
            if stmt_bal is not None:
                if current_bal is not None and current_bal < stmt_bal:
                    payment = max(current_bal, 0)
                else:
                    payment = stmt_bal
            else:
                payment = current_bal
        elif strategy == "min_payment":
            payment = min_pay if min_pay is not None else current_bal
        elif strategy == "current_balance":
            payment = current_bal
        else:
            payment = None

        due_date = liability["next_payment_due_date"] if liability else None

        cards.append({
            "name": card["name"],
            "current_balance": current_bal,
            "min_payment": min_pay,
            "statement_balance": stmt_bal,
            "payment_amount": payment,
            "due_date": due_date,
            "strategy": strategy,
        })

    cards.sort(key=lambda c: c["due_date"] or "9999")
    return cards


def render_digest(projection, cc_summary, detail_days, lowpoint_days, threshold):
    """Render the digest as an HTML string."""
    today = date.today()
    checking_balance = projection[0].opening_balance if projection else 0

    # 30-day detail table rows (only days with transactions)
    detail_rows = ""
    for day_data in projection[:detail_days + 1]:
        if day_data.transactions:
            txn_count = len(day_data.transactions)
            for i, txn in enumerate(day_data.transactions):
                is_first = i == 0
                is_last = i == txn_count - 1
                sign = "-" if txn.type == "debit" else "+"
                amt = f"{sign}${txn.amount:,.0f}"
                border = "border-bottom:1px solid #eee;" if is_last else ""
                date_cell = day_data.date.strftime('%a %b %-d') if is_first else ""
                bal_style = _balance_style(day_data.closing_balance, threshold) if is_last else ""
                bal_cell = f"${day_data.closing_balance:,.0f}" if is_last else ""
                detail_rows += f"""
                <tr>
                    <td style="padding:6px 12px;{border}">{date_cell}</td>
                    <td style="padding:6px 12px;{border}">{txn.name}</td>
                    <td style="padding:6px 12px;{border}text-align:right;color:{'#c0392b' if txn.type == 'debit' else '#27ae60'}">{amt}</td>
                    <td style="padding:6px 12px;{border}text-align:right;{bal_style}">{bal_cell}</td>
                </tr>"""

    # Low balance alerts
    lp = find_low_points(projection, threshold)
    below_t = lp["below_threshold"]
    below_z = lp["below_zero"]
    low_pt = lp["low_point"]

    def _lp_bullet(label, day):
        if day is None:
            return f"<li>{label}: <strong>N/A</strong></li>"
        return f"<li>{label}: {day.date.strftime('%a %b %-d')} — <strong>${day.closing_balance:,.0f}</strong></li>"

    alert_items = _lp_bullet(f"Below ${threshold:,.0f}", below_t)
    alert_items += _lp_bullet("Below $0", below_z)
    alert_items += _lp_bullet("Low point", low_pt)

    has_alerts = below_t is not None or below_z is not None
    if has_alerts:
        low_balance_section = f"""
        <div style="background:#fdf2f2;border-left:4px solid #c0392b;padding:12px 16px;margin:20px 0;">
            <strong style="color:#c0392b;">Low Balance Alerts (next {lowpoint_days} days)</strong>
            <ul>{alert_items}</ul>
        </div>"""
    else:
        low_balance_section = f"""
        <div style="background:#f0faf0;border-left:4px solid #27ae60;padding:12px 16px;margin:20px 0;">
            <strong style="color:#27ae60;">Balance Outlook (next {lowpoint_days} days)</strong>
            <ul>{alert_items}</ul>
        </div>"""

    # Credit card summary rows
    cc_rows = ""
    total_cc_debt = 0
    for card in cc_summary:
        bal = card["current_balance"]
        if bal is not None:
            total_cc_debt += bal
        cc_rows += f"""
        <tr>
            <td style="padding:6px 12px;border-bottom:1px solid #eee;">{card['name']}</td>
            <td style="padding:6px 12px;border-bottom:1px solid #eee;text-align:right;">{_fmt(bal)}</td>
            <td style="padding:6px 12px;border-bottom:1px solid #eee;text-align:right;">{_fmt(card['min_payment'])}</td>
            <td style="padding:6px 12px;border-bottom:1px solid #eee;text-align:right;">{_fmt(card['statement_balance'])}</td>
            <td style="padding:6px 12px;border-bottom:1px solid #eee;text-align:right;font-weight:bold;">{_fmt(card['payment_amount'])}</td>
            <td style="padding:6px 12px;border-bottom:1px solid #eee;text-align:center;">{_fmt_date(card['due_date'])}</td>
        </tr>"""

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family:-apple-system,Helvetica,Arial,sans-serif;max-width:700px;margin:0 auto;color:#333;">
    <h2 style="color:#2c3e50;border-bottom:2px solid #3498db;padding-bottom:8px;">
        Checking Projections &mdash; Week of {today.strftime('%b %-d, %Y')}
    </h2>

    <div style="background:#f7f9fc;padding:16px;border-radius:8px;margin:16px 0;">
        <span style="font-size:14px;color:#666;">Current Checking Balance</span><br>
        <span style="font-size:28px;font-weight:bold;color:#2c3e50;">${checking_balance:,.0f}</span>
    </div>

    <h3 style="color:#2c3e50;">30-Day Projection</h3>
    <table style="width:100%;border-collapse:collapse;font-size:14px;">
        <tr style="background:#f0f0f0;">
            <th style="padding:8px 12px;text-align:left;">Date</th>
            <th style="padding:8px 12px;text-align:left;">Transaction</th>
            <th style="padding:8px 12px;text-align:right;">Amount</th>
            <th style="padding:8px 12px;text-align:right;">Balance</th>
        </tr>
        {detail_rows}
    </table>

    {low_balance_section}

    <h3 style="color:#2c3e50;">Credit Card Summary</h3>
    <table style="width:100%;border-collapse:collapse;font-size:14px;">
        <tr style="background:#f0f0f0;">
            <th style="padding:8px 12px;text-align:left;">Card</th>
            <th style="padding:8px 12px;text-align:right;">Balance</th>
            <th style="padding:8px 12px;text-align:right;">Min Due</th>
            <th style="padding:8px 12px;text-align:right;">Stmt Bal</th>
            <th style="padding:8px 12px;text-align:right;">Expected Payment</th>
            <th style="padding:8px 12px;text-align:center;">Due Date</th>
        </tr>
        {cc_rows}
        <tr style="background:#f7f9fc;font-weight:bold;">
            <td style="padding:8px 12px;">Total</td>
            <td style="padding:8px 12px;text-align:right;">${total_cc_debt:,.0f}</td>
            <td colspan="4"></td>
        </tr>
    </table>

    <p style="color:#999;font-size:12px;margin-top:24px;">
        Generated on {today.strftime('%Y-%m-%d')} by Checking Projections
    </p>
</body>
</html>"""


def send_email(html, config):
    """Send the digest email via SMTP."""
    smtp_cfg = config["smtp"]
    today = date.today()

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Checking Projections - Week of {today.strftime('%b %-d, %Y')}"
    msg["From"] = smtp_cfg["from"]
    msg["To"] = smtp_cfg["to"]
    msg.attach(MIMEText(html, "html"))

    port = smtp_cfg["port"]
    if port == 465:
        with smtplib.SMTP_SSL(smtp_cfg["host"], port) as server:
            server.login(smtp_cfg["username"], smtp_cfg["password"])
            server.sendmail(smtp_cfg["from"], smtp_cfg["to"], msg.as_string())
    else:
        with smtplib.SMTP(smtp_cfg["host"], port) as server:
            server.starttls()
            server.login(smtp_cfg["username"], smtp_cfg["password"])
            server.sendmail(smtp_cfg["from"], smtp_cfg["to"], msg.as_string())

    log.info("Digest email sent to %s", smtp_cfg["to"])


def _fmt(value):
    """Format a dollar amount or return '—' if None."""
    if value is None:
        return "—"
    return f"${value:,.0f}"


def _fmt_date(value):
    """Format a date string as 'Fri Apr 10' or return '—' if None."""
    if not value:
        return "—"
    d = date.fromisoformat(str(value))
    return d.strftime("%a %b %-d")


def _balance_style(balance, threshold):
    """Return inline CSS for balance cells that are below threshold."""
    if balance < threshold:
        return "color:#c0392b;font-weight:bold;"
    return ""
