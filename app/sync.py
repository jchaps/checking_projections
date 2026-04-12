import json
import logging
import time

import plaid
from plaid.model.accounts_get_request import AccountsGetRequest
from plaid.model.liabilities_get_request import LiabilitiesGetRequest
from plaid.model.transactions_sync_request import TransactionsSyncRequest

from app import db, plaid_client, matcher

log = logging.getLogger(__name__)

# Plaid returns PRODUCT_NOT_READY briefly after an item is first linked, while
# transactions are still being pulled in the background. Retry with backoff.
_PRODUCT_NOT_READY_MAX_WAIT = 30  # seconds


def sync_all(config, conn, recurring_config):
    """Run a full sync cycle across all Plaid items."""
    tokens = plaid_client.load_tokens(config["data_dir"])
    client = plaid_client.create_client(config)

    checking_item = config["accounts"]["checking"]["plaid_item"]

    # Collect all unique items referenced in config
    items_needed = {checking_item}
    for card in config["accounts"]["credit_cards"]:
        items_needed.add(card["plaid_item"])

    for item_alias in items_needed:
        if item_alias not in tokens:
            log.warning("No access token for item '%s', skipping", item_alias)
            continue

        access_token = tokens[item_alias]
        try:
            # Sync transactions (checking account item only)
            if item_alias == checking_item:
                checking_account_id = _resolve_checking_account_id(
                    client, access_token, config["accounts"]["checking"]["account_name"],
                    config["accounts"]["checking"].get("account_mask"))
                _sync_transactions(client, conn, item_alias, access_token, checking_account_id)

            # Sync balances for all accounts under this item
            _sync_balances(client, conn, config, item_alias, access_token)

            # Sync liabilities for credit card items
            cards_for_item = [c for c in config["accounts"]["credit_cards"]
                              if c["plaid_item"] == item_alias]
            if cards_for_item:
                _sync_liabilities(client, conn, config, item_alias, access_token, cards_for_item)

        except Exception as e:
            log.exception("Error syncing item '%s'", item_alias)
            db.log_sync(conn, item_alias, error=str(e))

    # Run matcher after all syncs complete
    matcher.match_new_transactions(conn, recurring_config)


def _resolve_checking_account_id(client, access_token, account_name, account_mask=None):
    """Look up the Plaid account_id for the checking account."""
    request = AccountsGetRequest(access_token=access_token)
    response = client.accounts_get(request)
    for account in response.accounts:
        name = account.name or ""
        official_name = account.official_name or ""
        searchable = f"{name} {official_name}".lower()
        if account_name.lower() in searchable and str(account.type) == "depository":
            if account_mask and (account.mask or "") != account_mask:
                continue
            return account.account_id
    return None


def _transactions_sync_with_retry(client, request, item_alias):
    waited = 0.0
    delay = 1.0
    while True:
        try:
            return client.transactions_sync(request)
        except plaid.ApiException as e:
            body = json.loads(e.body) if e.body else {}
            if body.get("error_code") != "PRODUCT_NOT_READY" or waited >= _PRODUCT_NOT_READY_MAX_WAIT:
                raise
            log.info("PRODUCT_NOT_READY for '%s', retrying in %.0fs", item_alias, delay)
            time.sleep(delay)
            waited += delay
            delay = min(delay * 2, 5.0)


def _sync_transactions(client, conn, item_alias, access_token, checking_account_id=None):
    """Incrementally sync checking account transactions using /transactions/sync."""
    cursor = db.get_sync_cursor(conn, item_alias)
    has_more = True
    total_added = 0
    total_modified = 0
    total_removed = 0

    while has_more:
        request = TransactionsSyncRequest(access_token=access_token)
        if cursor:
            request.cursor = cursor

        response = _transactions_sync_with_retry(client, request, item_alias)

        # Process added — filter to checking account only
        added = []
        for txn in response.added:
            if checking_account_id and txn.account_id != checking_account_id:
                continue
            added.append({
                "id": txn.transaction_id,
                "date": str(txn.date),
                "name": txn.name,
                "amount": -txn.amount,
                "pending": txn.pending,
                "category": ", ".join(txn.category) if txn.category else None,
            })
        if added:
            db.upsert_transactions(conn, added)
            total_added += len(added)

        # Process modified — filter to checking account only
        modified = []
        for txn in response.modified:
            if checking_account_id and txn.account_id != checking_account_id:
                continue
            modified.append({
                "id": txn.transaction_id,
                "date": str(txn.date),
                "name": txn.name,
                "amount": -txn.amount,
                "pending": txn.pending,
                "category": ", ".join(txn.category) if txn.category else None,
            })
        if modified:
            db.upsert_transactions(conn, modified)
            total_modified += len(modified)

        # Process removed
        removed_ids = [txn.transaction_id for txn in response.removed]
        if removed_ids:
            db.remove_transactions(conn, removed_ids)
            total_removed += len(removed_ids)

        cursor = response.next_cursor
        has_more = response.has_more

    db.update_sync_cursor(conn, item_alias, cursor)
    db.log_sync(conn, item_alias, added=total_added, modified=total_modified, removed=total_removed)
    log.info("Synced transactions for '%s': +%d ~%d -%d",
             item_alias, total_added, total_modified, total_removed)


def _sync_balances(client, conn, config, item_alias, access_token):
    """Sync account balances for all accounts under this item."""
    request = AccountsGetRequest(access_token=access_token)
    response = client.accounts_get(request)

    checking_cfg = config["accounts"]["checking"]
    cards_cfg = config["accounts"]["credit_cards"]

    for account in response.accounts:
        name = account.name or ""
        official_name = account.official_name or ""
        searchable = f"{name} {official_name}".lower()

        # Check if this is the checking account
        checking_mask = checking_cfg.get("account_mask")
        if (item_alias == checking_cfg["plaid_item"]
                and checking_cfg["account_name"].lower() in searchable
                and (not checking_mask or (account.mask or "") == checking_mask)):
            db.upsert_balance(
                conn,
                account_id="checking",
                account_type="checking",
                current_balance=account.balances.current,
                available_balance=account.balances.available,
            )
            continue

        # Check if this matches a configured credit card
        # Sort by account_name length descending so "Venture X" matches before "Venture"
        for card in sorted(cards_cfg, key=lambda c: len(c["account_name"]), reverse=True):
            if (card["plaid_item"] == item_alias
                    and card["account_name"].lower() in searchable):
                db.upsert_balance(
                    conn,
                    account_id=card["name"],
                    account_type="credit_card",
                    current_balance=account.balances.current,
                )
                break


def _sync_liabilities(client, conn, config, item_alias, access_token, cards_for_item):
    """Sync credit card liability details."""
    request = LiabilitiesGetRequest(access_token=access_token)
    response = client.liabilities_get(request)

    credit_liabilities = response.liabilities.credit or []

    # Build a map of Plaid account_id -> our card config
    account_id_to_card = {}
    for account in response.accounts:
        name = account.name or ""
        official_name = account.official_name or ""
        searchable = f"{name} {official_name}".lower()
        for card in sorted(cards_for_item, key=lambda c: len(c["account_name"]), reverse=True):
            if card["account_name"].lower() in searchable:
                account_id_to_card[account.account_id] = card
                break

    for liability in credit_liabilities:
        card = account_id_to_card.get(liability.account_id)
        if card is None:
            continue

        db.upsert_liability(
            conn,
            account_id=card["name"],
            last_statement_balance=getattr(liability, "last_statement_balance", None),
            last_statement_date=str(liability.last_statement_issue_date) if getattr(liability, "last_statement_issue_date", None) else None,
            minimum_payment=getattr(liability, "minimum_payment_amount", None),
            next_payment_due_date=str(liability.next_payment_due_date) if getattr(liability, "next_payment_due_date", None) else None,
        )

    log.info("Synced liabilities for '%s': %d cards", item_alias, len(cards_for_item))
