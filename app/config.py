import os
import yaml


def load_config(path="config.yaml"):
    with open(path) as f:
        config = yaml.safe_load(f)

    # Validate required sections
    for section in ("plaid", "accounts", "smtp", "thresholds", "digest"):
        assert section in config, f"Missing config section: {section}"

    # Plaid credentials: env vars take precedence over YAML values
    plaid = config["plaid"]
    plaid["client_id"] = os.environ.get("PLAID_CLIENT_ID", plaid.get("client_id", ""))
    plaid["secret"] = os.environ.get("PLAID_SECRET", plaid.get("secret", ""))
    assert plaid["client_id"], "Plaid client_id not set (env PLAID_CLIENT_ID or plaid.client_id in config)"
    assert plaid["secret"], "Plaid secret not set (env PLAID_SECRET or plaid.secret in config)"

    assert "checking" in config["accounts"], "Missing accounts.checking"
    assert "credit_cards" in config["accounts"], "Missing accounts.credit_cards"

    # Validate each credit card has required fields
    for card in config["accounts"]["credit_cards"]:
        for field in ("name", "plaid_item", "account_name", "payment_strategy"):
            assert field in card, f"Credit card '{card.get('name', '?')}' missing field: {field}"
        assert card["payment_strategy"] in ("statement_balance", "min_payment", "current_balance"), (
            f"Invalid payment_strategy for {card['name']}: {card['payment_strategy']}"
        )

    # Defaults
    config.setdefault("data_dir", "./data")

    return config


def load_recurring(path="recurring.yaml"):
    with open(path) as f:
        data = yaml.safe_load(f)

    transactions = data.get("transactions", [])

    for txn in transactions:
        # Validate required fields
        for field in ("name", "type", "frequency", "match"):
            assert field in txn, f"Recurring '{txn.get('name', '?')}' missing field: {field}"

        assert txn["type"] in ("credit", "debit"), f"Invalid type for {txn['name']}: {txn['type']}"
        assert txn["frequency"] in ("monthly", "biweekly", "twice_monthly", "quarterly"), (
            f"Invalid frequency for {txn['name']}: {txn['frequency']}"
        )

        # Must have amount or amount_range
        assert "amount" in txn or "amount_range" in txn, (
            f"Recurring '{txn['name']}' needs 'amount' or 'amount_range'"
        )

        # Frequency-specific validation
        if txn["frequency"] == "monthly":
            assert "day" in txn, f"Monthly recurring '{txn['name']}' needs 'day'"
        elif txn["frequency"] == "biweekly":
            assert "anchor_date" in txn, f"Biweekly recurring '{txn['name']}' needs 'anchor_date'"
        elif txn["frequency"] == "twice_monthly":
            assert "days" in txn and len(txn["days"]) == 2, (
                f"Twice-monthly recurring '{txn['name']}' needs 'days' list of length 2"
            )
        elif txn["frequency"] == "quarterly":
            assert "day" in txn, f"Quarterly recurring '{txn['name']}' needs 'day'"
            assert "anchor_month" in txn, f"Quarterly recurring '{txn['name']}' needs 'anchor_month'"

        # Match block validation
        assert "name_contains" in txn["match"], (
            f"Recurring '{txn['name']}' match block needs 'name_contains'"
        )
        txn["match"].setdefault("amount_tolerance", 0)
        txn["match"].setdefault("date_tolerance_days", 3)

    return transactions


def get_projected_amount(recurring):
    """Get the amount to use for projections."""
    if "amount" in recurring:
        return recurring["amount"]
    low, high = recurring["amount_range"]
    return (low + high) / 2
