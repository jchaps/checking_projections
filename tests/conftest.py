import os
import sqlite3
import pytest

from app import db


@pytest.fixture
def conn(tmp_path):
    """In-memory SQLite database with schema applied."""
    c = db.get_db(str(tmp_path))
    yield c
    c.close()


@pytest.fixture
def sample_recurring():
    return [
        {
            "name": "Paycheck",
            "type": "credit",
            "amount": 3500.00,
            "frequency": "biweekly",
            "anchor_date": "2026-01-09",
            "match": {
                "name_contains": "DIRECT DEP",
                "amount_tolerance": 50.00,
                "date_tolerance_days": 3,
            },
        },
        {
            "name": "Rent",
            "type": "debit",
            "amount": 2100.00,
            "frequency": "monthly",
            "day": 1,
            "match": {
                "name_contains": "ZELLE",
                "amount_tolerance": 0,
                "date_tolerance_days": 3,
            },
        },
        {
            "name": "Electric",
            "type": "debit",
            "amount_range": [80, 180],
            "frequency": "monthly",
            "day": 15,
            "match": {
                "name_contains": "COMED",
                "amount_tolerance": 50.00,
                "date_tolerance_days": 3,
            },
        },
        {
            "name": "Internet",
            "type": "debit",
            "amount": 75.00,
            "frequency": "monthly",
            "day": 20,
            "match": {
                "name_contains": "FIOS",
                "amount_tolerance": 5.00,
                "date_tolerance_days": 3,
            },
        },
    ]


@pytest.fixture
def sample_config():
    return {
        "plaid": {
            "client_id": "test",
            "secret": "test",
            "environment": "sandbox",
        },
        "accounts": {
            "checking": {
                "plaid_item": "capital_one",
                "account_name": "360 Checking",
            },
            "credit_cards": [
                {
                    "name": "Savor",
                    "plaid_item": "capital_one",
                    "account_name": "Savor",
                    "payment_day": 15,
                    "payment_strategy": "statement_balance",
                },
                {
                    "name": "Venture",
                    "plaid_item": "capital_one",
                    "account_name": "Venture",
                    "payment_day": 22,
                    "payment_strategy": "min_payment",
                },
            ],
        },
        "smtp": {
            "host": "smtp.gmail.com",
            "port": 587,
            "username": "test@gmail.com",
            "password": "test",
            "from": "test@gmail.com",
            "to": "test@gmail.com",
        },
        "thresholds": {"low_balance_warning": 5000},
        "digest": {
            "projection_days_detail": 14,
            "projection_days_lowpoint": 60,
        },
        "data_dir": "./data",
    }
