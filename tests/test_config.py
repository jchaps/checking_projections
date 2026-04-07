import os
import pytest
import yaml

from app.config import load_config, load_recurring, get_projected_amount


@pytest.fixture
def config_path(tmp_path):
    """Write a valid config.yaml and return its path."""
    cfg = {
        "plaid": {"client_id": "id", "secret": "secret", "environment": "sandbox"},
        "accounts": {
            "checking": {"plaid_item": "cap1", "account_name": "360 Checking"},
            "credit_cards": [
                {
                    "name": "Savor",
                    "plaid_item": "cap1",
                    "account_name": "Savor",
                    "payment_day": 15,
                    "payment_strategy": "statement_balance",
                }
            ],
        },
        "smtp": {"host": "h", "port": 587, "username": "u", "password": "p", "from": "f", "to": "t"},
        "thresholds": {"low_balance_warning": 5000},
        "digest": {"projection_days_detail": 14, "projection_days_lowpoint": 60},
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.dump(cfg))
    return str(path)


@pytest.fixture
def recurring_path(tmp_path):
    """Write a valid recurring.yaml and return its path."""
    data = {
        "transactions": [
            {
                "name": "Rent",
                "type": "debit",
                "amount": 2100.00,
                "frequency": "monthly",
                "day": 1,
                "match": {"name_contains": "ZELLE"},
            },
            {
                "name": "Paycheck",
                "type": "credit",
                "amount": 3500.00,
                "frequency": "biweekly",
                "anchor_date": "2026-01-09",
                "match": {"name_contains": "DIRECT DEP", "amount_tolerance": 50},
            },
        ]
    }
    path = tmp_path / "recurring.yaml"
    path.write_text(yaml.dump(data))
    return str(path)


class TestLoadConfig:
    def test_valid_config(self, config_path):
        config = load_config(config_path)
        assert config["plaid"]["client_id"] == "id"
        assert config["data_dir"] == "./data"
        assert len(config["accounts"]["credit_cards"]) == 1

    def test_missing_section(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text(yaml.dump({"plaid": {}}))
        with pytest.raises(AssertionError, match="Missing config section"):
            load_config(str(path))

    def test_invalid_payment_strategy(self, tmp_path):
        cfg = {
            "plaid": {"client_id": "id", "secret": "s", "environment": "sandbox"},
            "accounts": {
                "checking": {"plaid_item": "c", "account_name": "C"},
                "credit_cards": [
                    {
                        "name": "Bad",
                        "plaid_item": "c",
                        "account_name": "Bad",
                        "payment_day": 1,
                        "payment_strategy": "yolo",
                    }
                ],
            },
            "smtp": {"host": "h", "port": 587, "username": "u", "password": "p", "from": "f", "to": "t"},
            "thresholds": {"low_balance_warning": 5000},
            "digest": {"projection_days_detail": 14, "projection_days_lowpoint": 60},
        }
        path = tmp_path / "config.yaml"
        path.write_text(yaml.dump(cfg))
        with pytest.raises(AssertionError, match="Invalid payment_strategy"):
            load_config(str(path))


class TestLoadRecurring:
    def test_valid_recurring(self, recurring_path):
        txns = load_recurring(recurring_path)
        assert len(txns) == 2
        assert txns[0]["name"] == "Rent"
        # Defaults applied
        assert txns[0]["match"]["amount_tolerance"] == 0
        assert txns[0]["match"]["date_tolerance_days"] == 3

    def test_missing_amount(self, tmp_path):
        data = {
            "transactions": [
                {
                    "name": "Bad",
                    "type": "debit",
                    "frequency": "monthly",
                    "day": 1,
                    "match": {"name_contains": "X"},
                }
            ]
        }
        path = tmp_path / "recurring.yaml"
        path.write_text(yaml.dump(data))
        with pytest.raises(AssertionError, match="needs 'amount' or 'amount_range'"):
            load_recurring(str(path))

    def test_invalid_frequency(self, tmp_path):
        data = {
            "transactions": [
                {
                    "name": "Bad",
                    "type": "debit",
                    "amount": 100,
                    "frequency": "weekly",
                    "match": {"name_contains": "X"},
                }
            ]
        }
        path = tmp_path / "recurring.yaml"
        path.write_text(yaml.dump(data))
        with pytest.raises(AssertionError, match="Invalid frequency"):
            load_recurring(str(path))


class TestGetProjectedAmount:
    def test_fixed_amount(self):
        assert get_projected_amount({"amount": 100.00}) == 100.00

    def test_amount_range_midpoint(self):
        assert get_projected_amount({"amount_range": [80, 180]}) == 130.00

    def test_fixed_takes_priority(self):
        assert get_projected_amount({"amount": 50, "amount_range": [80, 180]}) == 50
