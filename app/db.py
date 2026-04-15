import os
import sqlite3
from datetime import datetime

SCHEMA = """
CREATE TABLE IF NOT EXISTS sync_state (
    item_id TEXT PRIMARY KEY,
    cursor TEXT,
    last_synced_at TEXT
);

CREATE TABLE IF NOT EXISTS transactions (
    id TEXT PRIMARY KEY,
    date TEXT NOT NULL,
    name TEXT NOT NULL,
    amount REAL NOT NULL,
    pending INTEGER NOT NULL DEFAULT 0,
    category TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS account_balances (
    account_id TEXT PRIMARY KEY,
    account_type TEXT NOT NULL,
    current_balance REAL,
    available_balance REAL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cc_liabilities (
    account_id TEXT PRIMARY KEY,
    last_statement_balance REAL,
    last_statement_date TEXT,
    minimum_payment REAL,
    next_payment_due_date TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recurring_fulfillment (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recurring_name TEXT NOT NULL,
    cycle_date TEXT NOT NULL,
    matched_transaction_id TEXT,
    matched_at TEXT,
    UNIQUE(recurring_name, cycle_date)
);

CREATE TABLE IF NOT EXISTS sync_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id TEXT NOT NULL,
    synced_at TEXT NOT NULL,
    transactions_added INTEGER DEFAULT 0,
    transactions_modified INTEGER DEFAULT 0,
    transactions_removed INTEGER DEFAULT 0,
    error TEXT
);
"""


def get_db(data_dir):
    db_path = os.path.join(data_dir, "checking.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


# --- Transactions ---

def upsert_transactions(conn, transactions):
    """Insert or replace transactions from Plaid sync."""
    for txn in transactions:
        conn.execute(
            """INSERT OR REPLACE INTO transactions (id, date, name, amount, pending, category)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (txn["id"], txn["date"], txn["name"], txn["amount"],
             1 if txn.get("pending") else 0, txn.get("category")),
        )
    conn.commit()


def remove_transactions(conn, transaction_ids):
    """Remove transactions that Plaid reports as deleted."""
    for tid in transaction_ids:
        conn.execute("DELETE FROM transactions WHERE id = ?", (tid,))
    conn.commit()


def get_recent_unmatched_transactions(conn, days=30):
    """Get non-pending transactions from last N days that aren't matched to a recurring."""
    return conn.execute(
        """SELECT t.id, t.date, t.name, t.amount
           FROM transactions t
           WHERE t.pending = 0
             AND t.date >= date('now', ?)
             AND t.id NOT IN (SELECT matched_transaction_id FROM recurring_fulfillment WHERE matched_transaction_id IS NOT NULL)
           ORDER BY t.date""",
        (f"-{days} days",),
    ).fetchall()


def get_pending_transactions(conn):
    """Get currently pending transactions."""
    return conn.execute(
        "SELECT id, date, name, amount FROM transactions WHERE pending = 1 ORDER BY date"
    ).fetchall()


# --- Balances ---

def upsert_balance(conn, account_id, account_type, current_balance, available_balance=None):
    now = datetime.now().isoformat()
    conn.execute(
        """INSERT OR REPLACE INTO account_balances
           (account_id, account_type, current_balance, available_balance, updated_at)
           VALUES (?, ?, ?, ?, ?)""",
        (account_id, account_type, current_balance, available_balance, now),
    )
    conn.commit()


def get_checking_balance(conn):
    """Get the current checking account balance."""
    row = conn.execute(
        "SELECT current_balance, available_balance FROM account_balances WHERE account_type = 'checking'"
    ).fetchone()
    if row is None:
        return 0.0
    # Prefer available_balance for checking
    return row["available_balance"] if row["available_balance"] is not None else row["current_balance"]


def get_all_balances(conn):
    return conn.execute("SELECT * FROM account_balances ORDER BY account_type, account_id").fetchall()


# --- Credit Card Liabilities ---

def upsert_liability(conn, account_id, last_statement_balance, last_statement_date,
                     minimum_payment, next_payment_due_date):
    now = datetime.now().isoformat()
    conn.execute(
        """INSERT OR REPLACE INTO cc_liabilities
           (account_id, last_statement_balance, last_statement_date, minimum_payment,
            next_payment_due_date, updated_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (account_id, last_statement_balance, last_statement_date,
         minimum_payment, next_payment_due_date, now),
    )
    conn.commit()


def get_liability(conn, account_id):
    return conn.execute(
        "SELECT * FROM cc_liabilities WHERE account_id = ?", (account_id,)
    ).fetchone()


def get_all_liabilities(conn):
    # Sort by due date (earliest first); NULLs last, then account_id as tiebreaker.
    return conn.execute(
        """SELECT * FROM cc_liabilities
           ORDER BY CASE WHEN next_payment_due_date IS NULL THEN 1 ELSE 0 END,
                    next_payment_due_date,
                    account_id"""
    ).fetchall()


# --- Recurring Fulfillment ---

def is_fulfilled(conn, recurring_name, cycle_date):
    row = conn.execute(
        "SELECT 1 FROM recurring_fulfillment WHERE recurring_name = ? AND cycle_date = ?",
        (recurring_name, cycle_date),
    ).fetchone()
    return row is not None


def mark_fulfilled(conn, recurring_name, cycle_date, transaction_id):
    now = datetime.now().isoformat()
    conn.execute(
        """INSERT OR IGNORE INTO recurring_fulfillment
           (recurring_name, cycle_date, matched_transaction_id, matched_at)
           VALUES (?, ?, ?, ?)""",
        (recurring_name, cycle_date, transaction_id, now),
    )
    conn.commit()


def get_fulfillments(conn, days_back=60):
    return conn.execute(
        """SELECT * FROM recurring_fulfillment
           WHERE cycle_date >= date('now', ?)
           ORDER BY cycle_date""",
        (f"-{days_back} days",),
    ).fetchall()


# --- Sync State ---

def get_sync_cursor(conn, item_id):
    row = conn.execute("SELECT cursor FROM sync_state WHERE item_id = ?", (item_id,)).fetchone()
    return row["cursor"] if row else None


def update_sync_cursor(conn, item_id, cursor):
    now = datetime.now().isoformat()
    conn.execute(
        """INSERT OR REPLACE INTO sync_state (item_id, cursor, last_synced_at)
           VALUES (?, ?, ?)""",
        (item_id, cursor, now),
    )
    conn.commit()


def log_sync(conn, item_id, added=0, modified=0, removed=0, error=None):
    now = datetime.now().isoformat()
    conn.execute(
        """INSERT INTO sync_log (item_id, synced_at, transactions_added,
           transactions_modified, transactions_removed, error)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (item_id, now, added, modified, removed, error),
    )
    conn.commit()


def get_recent_syncs(conn, limit=20):
    return conn.execute(
        "SELECT * FROM sync_log ORDER BY synced_at DESC LIMIT ?", (limit,)
    ).fetchall()
