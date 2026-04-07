from datetime import date

from app.matcher import _try_match, _find_nearest_cycle_date


def _make_txn(name="DIRECT DEP PAYROLL", amount=3500.00, txn_date="2026-04-03"):
    """Default date 2026-04-03 lands on a biweekly cycle (anchor 2026-01-09 + 6*14)."""
    return {"id": "t1", "date": txn_date, "name": name, "amount": amount}


def _make_recurring_monthly(name="Rent", amount=2100.00, day=1, name_contains="ZELLE",
                            amount_tolerance=0, date_tolerance_days=3):
    return {
        "name": name,
        "type": "debit",
        "amount": amount,
        "frequency": "monthly",
        "day": day,
        "match": {
            "name_contains": name_contains,
            "amount_tolerance": amount_tolerance,
            "date_tolerance_days": date_tolerance_days,
        },
    }


def _make_recurring_biweekly(name="Paycheck", amount=3500.00, anchor="2026-01-09",
                              name_contains="DIRECT DEP", amount_tolerance=50,
                              date_tolerance_days=3):
    return {
        "name": name,
        "type": "credit",
        "amount": amount,
        "frequency": "biweekly",
        "anchor_date": anchor,
        "match": {
            "name_contains": name_contains,
            "amount_tolerance": amount_tolerance,
            "date_tolerance_days": date_tolerance_days,
        },
    }


class TestTryMatch:
    # --- Name matching ---

    def test_name_match_case_insensitive(self):
        txn = _make_txn(name="direct dep payroll")
        recurring = _make_recurring_biweekly()
        assert _try_match(txn, recurring) is not None

    def test_name_no_match(self):
        txn = _make_txn(name="VENMO PAYMENT")
        recurring = _make_recurring_biweekly()
        assert _try_match(txn, recurring) is None

    def test_name_substring_match(self):
        txn = _make_txn(name="ACH DIRECT DEP ACME CORP")
        recurring = _make_recurring_biweekly()
        assert _try_match(txn, recurring) is not None

    # --- Amount matching ---

    def test_exact_amount_match(self):
        txn = _make_txn(amount=3500.00)
        recurring = _make_recurring_biweekly(amount_tolerance=0)
        assert _try_match(txn, recurring) is not None

    def test_amount_within_tolerance(self):
        txn = _make_txn(amount=3520.00)
        recurring = _make_recurring_biweekly(amount_tolerance=50)
        assert _try_match(txn, recurring) is not None

    def test_amount_at_tolerance_boundary(self):
        txn = _make_txn(amount=3550.00)
        recurring = _make_recurring_biweekly(amount_tolerance=50)
        assert _try_match(txn, recurring) is not None

    def test_amount_exceeds_tolerance(self):
        txn = _make_txn(amount=3551.00)
        recurring = _make_recurring_biweekly(amount_tolerance=50)
        assert _try_match(txn, recurring) is None

    def test_negative_amount_matches(self):
        """Plaid credits have negative amounts; matcher uses abs()."""
        txn = _make_txn(amount=-3500.00)
        recurring = _make_recurring_biweekly(amount_tolerance=0)
        assert _try_match(txn, recurring) is not None

    # --- Date matching ---

    def test_date_exact_match(self):
        # 2026-01-09 + 6*14 = 2026-04-03
        txn = _make_txn(txn_date="2026-04-03")
        recurring = _make_recurring_biweekly(anchor="2026-01-09")
        assert _try_match(txn, recurring) == "2026-04-03"

    def test_date_within_tolerance(self):
        # 2026-04-05 is 2 days from cycle date 04-03
        txn = _make_txn(txn_date="2026-04-05")
        recurring = _make_recurring_biweekly(anchor="2026-01-09", date_tolerance_days=3)
        result = _try_match(txn, recurring)
        assert result is not None

    def test_date_exceeds_tolerance(self):
        # 2026-04-10 is 7 days from both 04-03 and 04-17
        txn = _make_txn(txn_date="2026-04-10")
        recurring = _make_recurring_biweekly(anchor="2026-01-09", date_tolerance_days=3)
        assert _try_match(txn, recurring) is None

    def test_monthly_match(self):
        txn = _make_txn(name="ZELLE PAYMENT", amount=2100.00, txn_date="2026-04-01")
        recurring = _make_recurring_monthly()
        assert _try_match(txn, recurring) == "2026-04-01"

    def test_monthly_off_by_one_day(self):
        txn = _make_txn(name="ZELLE PAYMENT", amount=2100.00, txn_date="2026-04-02")
        recurring = _make_recurring_monthly(date_tolerance_days=3)
        assert _try_match(txn, recurring) == "2026-04-01"


class TestFindNearestCycleDate:
    # --- Monthly ---

    def test_monthly_same_month(self):
        r = {"frequency": "monthly", "day": 15}
        assert _find_nearest_cycle_date(r, date(2026, 4, 14)) == date(2026, 4, 15)

    def test_monthly_previous_month_closer(self):
        r = {"frequency": "monthly", "day": 28}
        assert _find_nearest_cycle_date(r, date(2026, 4, 1)) == date(2026, 3, 28)

    def test_monthly_day_clamped_feb(self):
        r = {"frequency": "monthly", "day": 31}
        assert _find_nearest_cycle_date(r, date(2026, 2, 15)) == date(2026, 2, 28)

    def test_monthly_leap_year_feb(self):
        r = {"frequency": "monthly", "day": 29}
        assert _find_nearest_cycle_date(r, date(2028, 2, 28)) == date(2028, 2, 29)

    def test_monthly_year_boundary_dec_to_jan(self):
        r = {"frequency": "monthly", "day": 1}
        assert _find_nearest_cycle_date(r, date(2026, 12, 30)) == date(2027, 1, 1)

    def test_monthly_year_boundary_jan_to_dec(self):
        r = {"frequency": "monthly", "day": 28}
        assert _find_nearest_cycle_date(r, date(2027, 1, 2)) == date(2026, 12, 28)

    # --- Biweekly ---

    def test_biweekly_exact_cycle(self):
        r = {"frequency": "biweekly", "anchor_date": "2026-01-09"}
        # 2026-01-09 + 6*14 = 2026-04-03
        assert _find_nearest_cycle_date(r, date(2026, 4, 3)) == date(2026, 4, 3)

    def test_biweekly_rounds_to_nearest(self):
        r = {"frequency": "biweekly", "anchor_date": "2026-01-09"}
        # Between 2026-04-03 and 2026-04-17; 2026-04-08 is closer to 04-03
        assert _find_nearest_cycle_date(r, date(2026, 4, 8)) == date(2026, 4, 3)

    def test_biweekly_midpoint_rounds(self):
        r = {"frequency": "biweekly", "anchor_date": "2026-01-09"}
        # Exactly 7 days from 04-03 is 04-10; round(7/14)=round(0.5)=0 in Python (banker's rounding)
        result = _find_nearest_cycle_date(r, date(2026, 4, 10))
        assert result in (date(2026, 4, 3), date(2026, 4, 17))

    # --- Twice monthly ---

    def test_twice_monthly_first_day(self):
        r = {"frequency": "twice_monthly", "days": [1, 15]}
        assert _find_nearest_cycle_date(r, date(2026, 4, 2)) == date(2026, 4, 1)

    def test_twice_monthly_second_day(self):
        r = {"frequency": "twice_monthly", "days": [1, 15]}
        assert _find_nearest_cycle_date(r, date(2026, 4, 14)) == date(2026, 4, 15)

    def test_twice_monthly_between_days(self):
        r = {"frequency": "twice_monthly", "days": [5, 20]}
        # Apr 12 is equidistant from Apr 5 (7 days) and Apr 20 (8 days)
        assert _find_nearest_cycle_date(r, date(2026, 4, 12)) == date(2026, 4, 5)
