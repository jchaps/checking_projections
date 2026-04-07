from datetime import datetime

import pytest

from app import db


class TestTransactions:
    def test_upsert_and_query(self, conn):
        txns = [
            {"id": "t1", "date": "2026-04-01", "name": "ZELLE PAYMENT", "amount": 2100.00, "pending": False, "category": "Transfer"},
            {"id": "t2", "date": "2026-04-03", "name": "DIRECT DEP", "amount": -3500.00, "pending": False, "category": "Income"},
        ]
        db.upsert_transactions(conn, txns)
        rows = db.get_recent_unmatched_transactions(conn, days=30)
        assert len(rows) == 2

    def test_upsert_replaces_existing(self, conn):
        db.upsert_transactions(conn, [
            {"id": "t1", "date": "2026-04-01", "name": "OLD", "amount": 100, "pending": False, "category": None},
        ])
        db.upsert_transactions(conn, [
            {"id": "t1", "date": "2026-04-01", "name": "NEW", "amount": 200, "pending": False, "category": None},
        ])
        rows = db.get_recent_unmatched_transactions(conn, days=30)
        assert len(rows) == 1
        assert rows[0]["name"] == "NEW"
        assert rows[0]["amount"] == 200

    def test_remove_transactions(self, conn):
        db.upsert_transactions(conn, [
            {"id": "t1", "date": "2026-04-01", "name": "X", "amount": 100, "pending": False, "category": None},
            {"id": "t2", "date": "2026-04-02", "name": "Y", "amount": 200, "pending": False, "category": None},
        ])
        db.remove_transactions(conn, ["t1"])
        rows = db.get_recent_unmatched_transactions(conn, days=30)
        assert len(rows) == 1
        assert rows[0]["id"] == "t2"

    def test_pending_transactions(self, conn):
        db.upsert_transactions(conn, [
            {"id": "t1", "date": "2026-04-01", "name": "PENDING", "amount": 50, "pending": True, "category": None},
            {"id": "t2", "date": "2026-04-01", "name": "POSTED", "amount": 100, "pending": False, "category": None},
        ])
        pending = db.get_pending_transactions(conn)
        assert len(pending) == 1
        assert pending[0]["name"] == "PENDING"

    def test_unmatched_excludes_fulfilled(self, conn):
        db.upsert_transactions(conn, [
            {"id": "t1", "date": "2026-04-01", "name": "ZELLE", "amount": 2100, "pending": False, "category": None},
        ])
        db.mark_fulfilled(conn, "Rent", "2026-04-01", "t1")
        rows = db.get_recent_unmatched_transactions(conn, days=30)
        assert len(rows) == 0


class TestBalances:
    def test_upsert_and_get_checking(self, conn):
        db.upsert_balance(conn, "checking", "checking", 10000.00, 9500.00)
        assert db.get_checking_balance(conn) == 9500.00  # prefers available

    def test_checking_falls_back_to_current(self, conn):
        db.upsert_balance(conn, "checking", "checking", 10000.00, None)
        assert db.get_checking_balance(conn) == 10000.00

    def test_no_balance_returns_zero(self, conn):
        assert db.get_checking_balance(conn) == 0.0

    def test_get_all_balances(self, conn):
        db.upsert_balance(conn, "checking", "checking", 10000.00, 9500.00)
        db.upsert_balance(conn, "Savor", "credit_card", 500.00)
        rows = db.get_all_balances(conn)
        assert len(rows) == 2


class TestLiabilities:
    def test_upsert_and_get(self, conn):
        db.upsert_liability(conn, "Savor", 500.00, "2026-03-15", 25.00, "2026-04-15")
        row = db.get_liability(conn, "Savor")
        assert row["last_statement_balance"] == 500.00
        assert row["minimum_payment"] == 25.00

    def test_get_nonexistent(self, conn):
        assert db.get_liability(conn, "Nonexistent") is None

    def test_get_all(self, conn):
        db.upsert_liability(conn, "Savor", 500, "2026-03-15", 25, "2026-04-15")
        db.upsert_liability(conn, "Venture", 1200, "2026-03-20", 35, "2026-04-22")
        rows = db.get_all_liabilities(conn)
        assert len(rows) == 2


class TestFulfillment:
    def test_mark_and_check(self, conn):
        assert not db.is_fulfilled(conn, "Rent", "2026-04-01")
        db.mark_fulfilled(conn, "Rent", "2026-04-01", "t1")
        assert db.is_fulfilled(conn, "Rent", "2026-04-01")

    def test_duplicate_fulfillment_ignored(self, conn):
        db.mark_fulfilled(conn, "Rent", "2026-04-01", "t1")
        db.mark_fulfilled(conn, "Rent", "2026-04-01", "t2")  # should not raise
        rows = db.get_fulfillments(conn)
        rent_rows = [r for r in rows if r["recurring_name"] == "Rent" and r["cycle_date"] == "2026-04-01"]
        assert len(rent_rows) == 1
        assert rent_rows[0]["matched_transaction_id"] == "t1"  # first one wins

    def test_get_fulfillments(self, conn):
        db.mark_fulfilled(conn, "Rent", "2026-04-01", "t1")
        db.mark_fulfilled(conn, "Rent", "2026-05-01", "t2")
        rows = db.get_fulfillments(conn)
        assert len(rows) == 2


class TestSyncState:
    def test_cursor_lifecycle(self, conn):
        assert db.get_sync_cursor(conn, "cap1") is None
        db.update_sync_cursor(conn, "cap1", "cursor_abc")
        assert db.get_sync_cursor(conn, "cap1") == "cursor_abc"
        db.update_sync_cursor(conn, "cap1", "cursor_def")
        assert db.get_sync_cursor(conn, "cap1") == "cursor_def"

    def test_sync_log(self, conn):
        db.log_sync(conn, "cap1", added=5, modified=1, removed=0)
        db.log_sync(conn, "cap1", error="connection timeout")
        rows = db.get_recent_syncs(conn)
        assert len(rows) == 2
        assert rows[0]["error"] == "connection timeout"  # most recent first
        assert rows[1]["transactions_added"] == 5
