import calendar
from collections import namedtuple
from datetime import date, timedelta

from app import db
from app.config import get_projected_amount

ProjectedDay = namedtuple("ProjectedDay", ["date", "opening_balance", "transactions", "closing_balance"])
ProjectedTransaction = namedtuple("ProjectedTransaction", ["name", "amount", "type"])


def build_projection(conn, config, recurring_config, days=60):
    """Build a day-by-day balance projection for the checking account."""
    today = date.today()
    end_date = today + timedelta(days=days)

    balance = db.get_checking_balance(conn)

    # Generate all future events
    recurring_events = _generate_recurring_occurrences(recurring_config, today, end_date)
    cc_events = _generate_cc_payments(conn, config, today, end_date)
    all_future = recurring_events + cc_events

    # Remove fulfilled occurrences
    all_future = _filter_unfulfilled(all_future, conn)

    # Include pending transactions as events
    pending = db.get_pending_transactions(conn)
    pending_events = []
    for p in pending:
        txn_date = date.fromisoformat(p["date"])
        if today <= txn_date <= end_date:
            t = "credit" if p["amount"] > 0 else "debit"
            pending_events.append({
                "date": txn_date,
                "name": p["name"],
                "amount": abs(p["amount"]),
                "type": t,
                "recurring_name": None,
                "cycle_date": None,
            })

    all_events = sorted(all_future + pending_events, key=lambda e: e["date"])

    # Walk day by day
    projection = []
    running = balance
    for offset in range(days + 1):
        day = today + timedelta(days=offset)
        opening = running
        day_txns = []

        for event in all_events:
            if event["date"] == day:
                if event["type"] == "debit":
                    running -= event["amount"]
                else:
                    running += event["amount"]
                day_txns.append(ProjectedTransaction(
                    name=event["name"],
                    amount=event["amount"],
                    type=event["type"],
                ))

        projection.append(ProjectedDay(
            date=day,
            opening_balance=opening,
            transactions=day_txns,
            closing_balance=running,
        ))

    return projection


def find_low_points(projection, threshold):
    """Find key low-balance alert dates.

    Returns a dict with:
    - below_threshold: first day balance drops below threshold (or None)
    - below_zero: first day balance drops below zero (or None)
    - low_point: day with the lowest balance
    """
    first_below_threshold = None
    first_below_zero = None
    lowest_day = None

    for day in projection:
        if lowest_day is None or day.closing_balance < lowest_day.closing_balance:
            lowest_day = day
        if first_below_threshold is None and day.closing_balance < threshold:
            first_below_threshold = day
        if first_below_zero is None and day.closing_balance < 0:
            first_below_zero = day

    return {
        "below_threshold": first_below_threshold,
        "below_zero": first_below_zero,
        "low_point": lowest_day,
    }


def _generate_recurring_occurrences(recurring_config, start_date, end_date):
    """Expand recurring definitions into individual dated events."""
    events = []
    for r in recurring_config:
        dates = _get_occurrence_dates(r, start_date, end_date)
        amount = get_projected_amount(r)
        for d in dates:
            events.append({
                "date": d,
                "name": r["name"],
                "amount": amount,
                "type": r["type"],
                "recurring_name": r["name"],
                "cycle_date": str(d),
            })
    return events


def _get_occurrence_dates(recurring, start_date, end_date):
    """Get all occurrence dates for a recurring definition within a date range."""
    freq = recurring["frequency"]
    dates = []

    if freq == "monthly":
        day = recurring["day"]
        current = start_date.replace(day=1)
        while current <= end_date:
            max_day = calendar.monthrange(current.year, current.month)[1]
            occurrence = current.replace(day=min(day, max_day))
            if start_date <= occurrence <= end_date:
                dates.append(occurrence)
            # Next month
            if current.month == 12:
                current = current.replace(year=current.year + 1, month=1)
            else:
                current = current.replace(month=current.month + 1)

    elif freq == "biweekly":
        anchor = date.fromisoformat(str(recurring["anchor_date"]))
        # Find the first occurrence on or after start_date
        days_since = (start_date - anchor).days
        cycles = max(0, days_since // 14)
        current = anchor + timedelta(days=cycles * 14)
        if current < start_date:
            current += timedelta(days=14)
        while current <= end_date:
            dates.append(current)
            current += timedelta(days=14)

    elif freq == "twice_monthly":
        day_list = recurring["days"]
        current = start_date.replace(day=1)
        while current <= end_date:
            for day in day_list:
                max_day = calendar.monthrange(current.year, current.month)[1]
                occurrence = current.replace(day=min(day, max_day))
                if start_date <= occurrence <= end_date:
                    dates.append(occurrence)
            if current.month == 12:
                current = current.replace(year=current.year + 1, month=1)
            else:
                current = current.replace(month=current.month + 1)

    elif freq == "quarterly":
        anchor_month = recurring["anchor_month"]
        day = recurring["day"]
        # Generate quarterly months (anchor, anchor+3, anchor+6, anchor+9)
        quarter_months = [(anchor_month + 3 * i - 1) % 12 + 1 for i in range(4)]
        current = start_date.replace(day=1)
        while current <= end_date:
            if current.month in quarter_months:
                max_day = calendar.monthrange(current.year, current.month)[1]
                occurrence = current.replace(day=min(day, max_day))
                if start_date <= occurrence <= end_date:
                    dates.append(occurrence)
            if current.month == 12:
                current = current.replace(year=current.year + 1, month=1)
            else:
                current = current.replace(month=current.month + 1)

    return sorted(dates)


def _generate_cc_payments(conn, config, start_date, end_date):
    """Generate credit card payment events from liability due dates.

    Walks projected payment dates in order, drawing down a running `remaining`
    balance so subsequent in-window payments reflect prior payments already
    accounted for in the projection.
    """
    events = []
    for card in config["accounts"]["credit_cards"]:
        liability = db.get_liability(conn, card["name"])
        if not liability or not liability["next_payment_due_date"]:
            continue

        balance_row = conn.execute(
            "SELECT current_balance FROM account_balances WHERE account_id = ?",
            (card["name"],)
        ).fetchone()
        current_balance = balance_row["current_balance"] if balance_row else None

        due_date = date.fromisoformat(liability["next_payment_due_date"])

        # Project due dates: current cycle and future months at same day-of-month
        payment_day = due_date.day
        candidates = [due_date]
        current = due_date
        for _ in range(3):  # up to 3 more months ahead
            if current.month == 12:
                current = current.replace(year=current.year + 1, month=1)
            else:
                current = current.replace(month=current.month + 1)
            max_day = calendar.monthrange(current.year, current.month)[1]
            candidates.append(current.replace(day=min(payment_day, max_day)))

        strategy = card["payment_strategy"]
        statement_bal = liability["last_statement_balance"]
        min_payment = liability["minimum_payment"]
        remaining = current_balance

        # Plaid reports minimum_payment <= 0 once the cycle's payment has posted;
        # skip the current due date so we don't double-count a payment that's
        # already cleared in checking.
        cycle_paid = min_payment is not None and min_payment <= 0

        for idx, pay_date in enumerate(candidates):
            if idx == 0 and cycle_paid:
                continue
            amount = _compute_payment_amount(
                strategy, idx, remaining, statement_bal, min_payment
            )
            if amount is None or amount <= 0:
                continue
            if start_date <= pay_date <= end_date:
                events.append({
                    "date": pay_date,
                    "name": f"CC Payment: {card['name']}",
                    "amount": amount,
                    "type": "debit",
                    "recurring_name": f"__cc__{card['name']}",
                    "cycle_date": str(pay_date),
                })
            if remaining is not None:
                remaining = max(0, remaining - amount)

    return events


def _compute_payment_amount(strategy, idx, remaining, statement_bal, min_payment):
    """Compute payment amount for one CC payment occurrence.

    `idx` is position in the projected series (0 = next due payment). For
    statement_balance and current_balance strategies, later payments draw down
    `remaining` rather than re-paying the original balance.
    """
    if strategy == "statement_balance":
        if idx == 0:
            if statement_bal is not None:
                if remaining is not None and remaining < statement_bal:
                    return max(remaining, 0)
                return statement_bal
            return remaining
        return remaining

    if strategy == "min_payment":
        if min_payment is not None:
            return min_payment
        return remaining

    if strategy == "current_balance":
        return remaining

    return None


def _filter_unfulfilled(events, conn):
    """Remove events that have already been matched to posted transactions."""
    result = []
    for event in events:
        rname = event.get("recurring_name")
        cdate = event.get("cycle_date")
        if rname and cdate and db.is_fulfilled(conn, rname, cdate):
            continue
        result.append(event)
    return result
