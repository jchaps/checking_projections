import logging
from datetime import date, timedelta

from app import db
from app.config import get_projected_amount

log = logging.getLogger(__name__)


def match_new_transactions(conn, recurring_config):
    """Scan recent unmatched transactions and try to match them to recurring definitions."""
    recent_txns = db.get_recent_unmatched_transactions(conn, days=30)
    matched_count = 0

    for txn in recent_txns:
        for recurring in recurring_config:
            match_result = _try_match(txn, recurring)
            if match_result is None:
                continue

            cycle_date = match_result
            if db.is_fulfilled(conn, recurring["name"], cycle_date):
                continue

            db.mark_fulfilled(conn, recurring["name"], cycle_date, txn["id"])
            log.info("Matched '%s' (%s) -> recurring '%s' cycle %s",
                     txn["name"], txn["date"], recurring["name"], cycle_date)
            matched_count += 1
            break  # one transaction matches at most one recurring

    if matched_count:
        log.info("Matched %d transactions to recurring definitions", matched_count)


def _try_match(txn, recurring):
    """Try to match a transaction to a recurring definition.

    Returns the cycle_date string if matched, None otherwise.
    """
    match_cfg = recurring["match"]

    # 1. Name match: case-insensitive substring
    if match_cfg["name_contains"].lower() not in txn["name"].lower():
        return None

    # 2. Amount match: within tolerance
    tolerance = match_cfg.get("amount_tolerance", 0)
    expected = get_projected_amount(recurring)
    # Compare absolute values (credits positive, debits negative).
    if abs(abs(txn["amount"]) - expected) > tolerance:
        return None

    # 3. Date match: find nearest expected cycle date
    txn_date = date.fromisoformat(txn["date"])
    cycle_date = _find_nearest_cycle_date(recurring, txn_date)
    if cycle_date is None:
        return None

    date_tolerance = match_cfg.get("date_tolerance_days", 3)
    if abs((txn_date - cycle_date).days) > date_tolerance:
        return None

    return str(cycle_date)


def _find_nearest_cycle_date(recurring, target_date):
    """Find the expected cycle date closest to the target date."""
    freq = recurring["frequency"]

    if freq == "monthly":
        day = recurring["day"]
        # Try this month and adjacent months
        candidates = []
        for month_offset in (-1, 0, 1):
            try:
                m = target_date.month + month_offset
                y = target_date.year
                if m < 1:
                    m += 12
                    y -= 1
                elif m > 12:
                    m -= 12
                    y += 1
                # Clamp day to valid range for the month
                import calendar
                max_day = calendar.monthrange(y, m)[1]
                candidates.append(date(y, m, min(day, max_day)))
            except ValueError:
                continue
        return min(candidates, key=lambda d: abs((d - target_date).days)) if candidates else None

    elif freq == "biweekly":
        anchor = date.fromisoformat(str(recurring["anchor_date"]))
        days_since_anchor = (target_date - anchor).days
        # Find nearest multiple of 14
        cycles = round(days_since_anchor / 14)
        return anchor + timedelta(days=cycles * 14)

    elif freq == "quarterly":
        anchor_month = recurring["anchor_month"]
        day = recurring["day"]
        quarter_months = [(anchor_month + 3 * i - 1) % 12 + 1 for i in range(4)]
        candidates = []
        for month_offset in (-3, 0, 3):
            m = target_date.month + month_offset
            y = target_date.year
            if m < 1:
                m += 12
                y -= 1
            elif m > 12:
                m -= 12
                y += 1
            if m in quarter_months:
                try:
                    import calendar
                    max_day = calendar.monthrange(y, m)[1]
                    candidates.append(date(y, m, min(day, max_day)))
                except ValueError:
                    continue
        return min(candidates, key=lambda d: abs((d - target_date).days)) if candidates else None

    elif freq == "twice_monthly":
        days = recurring["days"]
        candidates = []
        for month_offset in (-1, 0, 1):
            for day in days:
                try:
                    m = target_date.month + month_offset
                    y = target_date.year
                    if m < 1:
                        m += 12
                        y -= 1
                    elif m > 12:
                        m -= 12
                        y += 1
                    import calendar
                    max_day = calendar.monthrange(y, m)[1]
                    candidates.append(date(y, m, min(day, max_day)))
                except ValueError:
                    continue
        return min(candidates, key=lambda d: abs((d - target_date).days)) if candidates else None

    return None
