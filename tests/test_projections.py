from datetime import date, timedelta
from unittest.mock import patch

import pytest

from app import db
from app.projections import (
    build_projection,
    find_low_points,
    _generate_recurring_occurrences,
    _get_occurrence_dates,
    _generate_cc_payments,
    _get_payment_amount,
    _filter_unfulfilled,
    ProjectedDay,
    ProjectedTransaction,
)


class TestGetOccurrenceDates:
    # --- Monthly ---

    def test_monthly_single_month(self):
        r = {"frequency": "monthly", "day": 15}
        dates = _get_occurrence_dates(r, date(2026, 4, 1), date(2026, 4, 30))
        assert dates == [date(2026, 4, 15)]

    def test_monthly_spans_months(self):
        r = {"frequency": "monthly", "day": 1}
        dates = _get_occurrence_dates(r, date(2026, 4, 1), date(2026, 6, 30))
        assert dates == [date(2026, 4, 1), date(2026, 5, 1), date(2026, 6, 1)]

    def test_monthly_day_clamped(self):
        r = {"frequency": "monthly", "day": 31}
        dates = _get_occurrence_dates(r, date(2026, 2, 1), date(2026, 2, 28))
        assert dates == [date(2026, 2, 28)]

    def test_monthly_excludes_out_of_range(self):
        r = {"frequency": "monthly", "day": 5}
        dates = _get_occurrence_dates(r, date(2026, 4, 10), date(2026, 4, 30))
        assert dates == []

    def test_monthly_year_boundary(self):
        r = {"frequency": "monthly", "day": 15}
        dates = _get_occurrence_dates(r, date(2026, 12, 1), date(2027, 1, 31))
        assert dates == [date(2026, 12, 15), date(2027, 1, 15)]

    # --- Biweekly ---

    def test_biweekly_basic(self):
        r = {"frequency": "biweekly", "anchor_date": "2026-01-09"}
        dates = _get_occurrence_dates(r, date(2026, 4, 1), date(2026, 4, 30))
        # From anchor 01-09: +84d=04-03, +98d=04-17
        assert dates == [date(2026, 4, 3), date(2026, 4, 17)]

    def test_biweekly_anchor_in_future(self):
        r = {"frequency": "biweekly", "anchor_date": "2026-05-01"}
        dates = _get_occurrence_dates(r, date(2026, 4, 1), date(2026, 4, 30))
        # Anchor is after end_date; current implementation walks forward from
        # anchor so no dates land in range — this is expected behavior
        assert dates == []

    def test_biweekly_single_occurrence(self):
        r = {"frequency": "biweekly", "anchor_date": "2026-01-09"}
        dates = _get_occurrence_dates(r, date(2026, 4, 3), date(2026, 4, 10))
        assert dates == [date(2026, 4, 3)]

    # --- Twice monthly ---

    def test_twice_monthly_both_days(self):
        r = {"frequency": "twice_monthly", "days": [5, 20]}
        dates = _get_occurrence_dates(r, date(2026, 4, 1), date(2026, 4, 30))
        assert dates == [date(2026, 4, 5), date(2026, 4, 20)]

    def test_twice_monthly_one_in_range(self):
        r = {"frequency": "twice_monthly", "days": [5, 20]}
        dates = _get_occurrence_dates(r, date(2026, 4, 10), date(2026, 4, 25))
        assert dates == [date(2026, 4, 20)]


class TestGenerateRecurringOccurrences:
    def test_generates_events(self):
        recurring = [
            {"name": "Rent", "type": "debit", "amount": 2100, "frequency": "monthly", "day": 1,
             "match": {"name_contains": "ZELLE"}},
        ]
        events = _generate_recurring_occurrences(recurring, date(2026, 4, 1), date(2026, 5, 31))
        assert len(events) == 2
        assert events[0]["name"] == "Rent"
        assert events[0]["amount"] == 2100
        assert events[0]["type"] == "debit"
        assert events[0]["recurring_name"] == "Rent"

    def test_empty_recurring(self):
        events = _generate_recurring_occurrences([], date(2026, 4, 1), date(2026, 4, 30))
        assert events == []

    def test_amount_range_uses_midpoint(self):
        recurring = [
            {"name": "Electric", "type": "debit", "amount_range": [80, 180],
             "frequency": "monthly", "day": 15, "match": {"name_contains": "COMED"}},
        ]
        events = _generate_recurring_occurrences(recurring, date(2026, 4, 1), date(2026, 4, 30))
        assert events[0]["amount"] == 130.0


class TestGenerateCcPayments:
    def test_generates_payment_events(self, conn, sample_config):
        db.upsert_liability(conn, "Savor", 500.00, "2026-03-15", 25.00, "2026-04-15")
        events = _generate_cc_payments(conn, sample_config, date(2026, 4, 1), date(2026, 4, 30))
        savor_events = [e for e in events if "Savor" in e["name"]]
        assert len(savor_events) == 1
        assert savor_events[0]["amount"] == 500.00  # statement_balance strategy
        assert savor_events[0]["type"] == "debit"

    def test_min_payment_strategy(self, conn, sample_config):
        db.upsert_liability(conn, "Venture", 1200.00, "2026-03-20", 35.00, "2026-04-22")
        events = _generate_cc_payments(conn, sample_config, date(2026, 4, 1), date(2026, 4, 30))
        venture_events = [e for e in events if "Venture" in e["name"]]
        assert len(venture_events) == 1
        assert venture_events[0]["amount"] == 35.00  # min_payment strategy

    def test_skips_zero_payment(self, conn, sample_config):
        db.upsert_liability(conn, "Savor", 0, "2026-03-15", 0, "2026-04-15")
        events = _generate_cc_payments(conn, sample_config, date(2026, 4, 1), date(2026, 4, 30))
        savor_events = [e for e in events if "Savor" in e["name"]]
        assert len(savor_events) == 0

    def test_no_liability_data(self, conn, sample_config):
        events = _generate_cc_payments(conn, sample_config, date(2026, 4, 1), date(2026, 4, 30))
        assert events == []


class TestGetPaymentAmount:
    def test_statement_balance(self, conn):
        card = {"name": "TestCard", "payment_strategy": "statement_balance"}
        db.upsert_liability(conn, "TestCard", 800.00, "2026-03-15", 25.00, "2026-04-15")
        assert _get_payment_amount(conn, card) == 800.00

    def test_min_payment(self, conn):
        card = {"name": "TestCard", "payment_strategy": "min_payment"}
        db.upsert_liability(conn, "TestCard", 800.00, "2026-03-15", 25.00, "2026-04-15")
        assert _get_payment_amount(conn, card) == 25.00

    def test_current_balance(self, conn):
        card = {"name": "TestCard", "payment_strategy": "current_balance"}
        db.upsert_balance(conn, "TestCard", "credit_card", 950.00)
        assert _get_payment_amount(conn, card) == 950.00

    def test_statement_balance_falls_back_to_current(self, conn):
        card = {"name": "TestCard", "payment_strategy": "statement_balance"}
        db.upsert_balance(conn, "TestCard", "credit_card", 600.00)
        # No liability record -> falls back to current_balance
        assert _get_payment_amount(conn, card) == 600.00

    def test_no_data_returns_none(self, conn):
        card = {"name": "TestCard", "payment_strategy": "statement_balance"}
        assert _get_payment_amount(conn, card) is None


class TestFilterUnfulfilled:
    def test_removes_fulfilled(self, conn):
        db.mark_fulfilled(conn, "Rent", "2026-04-01", "t1")
        events = [
            {"date": date(2026, 4, 1), "name": "Rent", "recurring_name": "Rent", "cycle_date": "2026-04-01"},
            {"date": date(2026, 5, 1), "name": "Rent", "recurring_name": "Rent", "cycle_date": "2026-05-01"},
        ]
        result = _filter_unfulfilled(events, conn)
        assert len(result) == 1
        assert result[0]["cycle_date"] == "2026-05-01"

    def test_keeps_events_without_recurring_name(self, conn):
        events = [{"date": date(2026, 4, 5), "name": "Random", "recurring_name": None, "cycle_date": None}]
        result = _filter_unfulfilled(events, conn)
        assert len(result) == 1


class TestFindLowPoints:
    def test_finds_below_threshold(self):
        projection = [
            ProjectedDay(date(2026, 4, 1), 6000, [], 6000),
            ProjectedDay(date(2026, 4, 2), 6000, [ProjectedTransaction("Rent", 2100, "debit")], 3900),
            ProjectedDay(date(2026, 4, 3), 3900, [], 3900),
        ]
        alerts = find_low_points(projection, 5000)
        assert len(alerts) == 2
        assert alerts[0].date == date(2026, 4, 2)
        assert alerts[1].date == date(2026, 4, 3)

    def test_no_alerts_when_above_threshold(self):
        projection = [
            ProjectedDay(date(2026, 4, 1), 10000, [], 10000),
            ProjectedDay(date(2026, 4, 2), 10000, [], 10000),
        ]
        assert find_low_points(projection, 5000) == []


class TestBuildProjection:
    @patch("app.projections.date")
    def test_basic_projection(self, mock_date, conn, sample_config):
        mock_date.today.return_value = date(2026, 4, 6)
        mock_date.fromisoformat = date.fromisoformat
        mock_date.side_effect = lambda *a, **kw: date(*a, **kw)

        db.upsert_balance(conn, "checking", "checking", 10000.00, 10000.00)

        recurring = [
            {"name": "Rent", "type": "debit", "amount": 2100, "frequency": "monthly", "day": 1,
             "match": {"name_contains": "ZELLE", "amount_tolerance": 0, "date_tolerance_days": 3}},
        ]

        projection = build_projection(conn, sample_config, recurring, days=30)
        assert len(projection) == 31  # today + 30 days
        assert projection[0].opening_balance == 10000.00

        # Rent on May 1 (day index 25)
        may1 = [d for d in projection if d.date == date(2026, 5, 1)]
        assert len(may1) == 1
        assert any(t.name == "Rent" for t in may1[0].transactions)

    @patch("app.projections.date")
    def test_projection_with_fulfilled_excluded(self, mock_date, conn, sample_config):
        mock_date.today.return_value = date(2026, 4, 6)
        mock_date.fromisoformat = date.fromisoformat
        mock_date.side_effect = lambda *a, **kw: date(*a, **kw)

        db.upsert_balance(conn, "checking", "checking", 10000.00, 10000.00)
        db.mark_fulfilled(conn, "Rent", "2026-05-01", "t1")

        recurring = [
            {"name": "Rent", "type": "debit", "amount": 2100, "frequency": "monthly", "day": 1,
             "match": {"name_contains": "ZELLE", "amount_tolerance": 0, "date_tolerance_days": 3}},
        ]

        projection = build_projection(conn, sample_config, recurring, days=30)
        may1 = [d for d in projection if d.date == date(2026, 5, 1)]
        assert len(may1) == 1
        assert not any(t.name == "Rent" for t in may1[0].transactions)
