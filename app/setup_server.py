"""Browser-based setup wizard for Checking Projections."""

import json
import logging
import os
import smtplib
import ssl
import webbrowser
from email.mime.text import MIMEText
from pathlib import Path

import yaml
from cryptography.fernet import Fernet
from flask import Flask, jsonify, request, send_from_directory

from app import plaid_client, db

log = logging.getLogger(__name__)

PORT = 8485
BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = Path(__file__).resolve().parent / "static"

# In-memory state held during the setup wizard session.
_state = {
    "plaid_config": None,   # {client_id, secret, environment}
    "plaid_client": None,   # PlaidApi instance
    "encryption_key": None, # Fernet key generated during setup
    "data_dir": "./data",
}

app = Flask(__name__, static_folder=str(STATIC_DIR))


# ── Page ─────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory(STATIC_DIR, "setup.html")


# ── Load existing config ──────────────────────────────────────────────

@app.route("/api/load-config")
def load_existing_config():
    """Load existing configuration files and linked accounts for editing."""
    config_path = BASE_DIR / "config.yaml"
    recurring_path = BASE_DIR / "recurring.yaml"
    env_path = BASE_DIR / ".env"

    if not config_path.exists():
        return jsonify({"ok": False, "reason": "no_config"})

    try:
        with open(config_path) as f:
            config = yaml.safe_load(f)

        # Read Plaid credentials from .env
        plaid_client_id = ""
        plaid_secret = ""
        encryption_key = ""
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if line.startswith("PLAID_CLIENT_ID="):
                    plaid_client_id = line.split("=", 1)[1]
                elif line.startswith("PLAID_SECRET="):
                    plaid_secret = line.split("=", 1)[1]
                elif line.startswith("PLAID_ENCRYPTION_KEY="):
                    encryption_key = line.split("=", 1)[1]

        # Also check env vars (Docker passes them directly)
        plaid_client_id = plaid_client_id or os.environ.get("PLAID_CLIENT_ID", "")
        plaid_secret = plaid_secret or os.environ.get("PLAID_SECRET", "")
        encryption_key = encryption_key or os.environ.get("PLAID_ENCRYPTION_KEY", "")

        # Fall back to config.yaml values for backward compat
        plaid_cfg = config.get("plaid", {})
        plaid_client_id = plaid_client_id or plaid_cfg.get("client_id", "")
        plaid_secret = plaid_secret or plaid_cfg.get("secret", "")
        environment = plaid_cfg.get("environment", "development")

        # Establish Plaid client and encryption key on the server
        if encryption_key:
            _state["encryption_key"] = encryption_key
            os.environ["PLAID_ENCRYPTION_KEY"] = encryption_key

        linked_items = {}
        if plaid_client_id and plaid_secret:
            config_stub = {
                "plaid": {
                    "client_id": plaid_client_id,
                    "secret": plaid_secret,
                    "environment": environment,
                }
            }
            try:
                client = plaid_client.create_client(config_stub)
                _state["plaid_config"] = config_stub["plaid"]
                _state["plaid_client"] = client

                data_dir = config.get("data_dir", "./data")
                tokens = plaid_client.load_tokens(data_dir)
                for item_alias, access_token in tokens.items():
                    try:
                        accounts = plaid_client.list_accounts(client, access_token)
                        linked_items[item_alias] = [
                            {
                                "name": acct.name or "",
                                "official_name": acct.official_name or "",
                                "type": str(acct.type) if acct.type else "",
                                "subtype": str(acct.subtype) if acct.subtype else "",
                                "mask": acct.mask or "",
                            }
                            for acct in accounts
                        ]
                    except Exception as e:
                        log.warning("Failed to list accounts for %s: %s", item_alias, e)
            except Exception as e:
                log.warning("Failed to create Plaid client from existing config: %s", e)

        # Load recurring
        recurring = []
        if recurring_path.exists():
            with open(recurring_path) as f:
                rdata = yaml.safe_load(f)
            recurring = rdata.get("transactions", []) if rdata else []

        # Build response matching frontend state shape
        accounts_cfg = config.get("accounts", {})
        checking_cfg = accounts_cfg.get("checking", {})
        checking = None
        if checking_cfg.get("account_name"):
            # Match config account_name to actual Plaid account for consistent _key
            chk_item = checking_cfg.get("plaid_item", "")
            chk_name = checking_cfg.get("account_name", "")
            chk_mask = checking_cfg.get("account_mask", "")
            matched_name = chk_name
            matched_mask = chk_mask
            for acct in linked_items.get(chk_item, []):
                acct_searchable = (acct.get("name", "") + " " + acct.get("official_name", "")).lower()
                if chk_name.lower() in acct_searchable and str(acct.get("type", "")) == "depository":
                    if chk_mask and acct.get("mask", "") != chk_mask:
                        continue
                    matched_name = acct["name"]
                    matched_mask = acct.get("mask", "")
                    break
            checking = {
                "plaid_item": chk_item,
                "account_name": matched_name,
                "account_mask": matched_mask,
                "_key": chk_item + "|" + matched_name + "|" + matched_mask,
            }

        credit_cards = []
        seen_keys = set()
        for card in accounts_cfg.get("credit_cards", []):
            # Match config account_name (substring) to actual Plaid account
            # to build a _key consistent with the frontend
            matched_key = card["plaid_item"] + "|" + card["account_name"] + "|"
            matched_mask = card.get("account_mask", "")
            item_accounts = linked_items.get(card["plaid_item"], [])
            # Sort by name length descending so longer matches win
            for acct in sorted(item_accounts, key=lambda a: len(a.get("name", "")), reverse=True):
                if card["account_name"].lower() in (acct.get("name", "") + " " + acct.get("official_name", "")).lower():
                    if matched_mask and acct.get("mask", "") != matched_mask:
                        continue
                    matched_key = card["plaid_item"] + "|" + acct["name"] + "|" + acct.get("mask", "")
                    matched_mask = acct.get("mask", "")
                    break
            # Skip duplicates
            if matched_key in seen_keys:
                continue
            seen_keys.add(matched_key)
            credit_cards.append({
                "name": card.get("name", card["account_name"]),
                "plaid_item": card["plaid_item"],
                "account_name": card["account_name"],
                "account_mask": matched_mask,
                "payment_strategy": card.get("payment_strategy", "statement_balance"),
                "_key": matched_key,
            })

        # Convert recurring to frontend format
        fe_recurring = []
        for r in recurring:
            entry = {
                "name": r.get("name", ""),
                "type": r.get("type", "debit"),
                "frequency": r.get("frequency", "monthly"),
                "day": r.get("day", 1),
                "anchor_date": str(r.get("anchor_date", "")),
                "days": r.get("days", [1, 15]),
                "anchor_month": r.get("anchor_month", 1),
                "useRange": "amount_range" in r,
                "amount": r.get("amount", ""),
                "amount_range_low": r["amount_range"][0] if "amount_range" in r else "",
                "amount_range_high": r["amount_range"][1] if "amount_range" in r else "",
                "match": {
                    "name_contains": r.get("match", {}).get("name_contains", ""),
                    "amount_tolerance": r.get("match", {}).get("amount_tolerance", 0),
                    "date_tolerance_days": r.get("match", {}).get("date_tolerance_days", 3),
                },
            }
            fe_recurring.append(entry)

        return jsonify({
            "ok": True,
            "state": {
                "plaid": {
                    "client_id": plaid_client_id,
                    "secret": plaid_secret,
                    "environment": environment,
                },
                "plaidValidated": bool(plaid_client_id and plaid_secret),
                "encryptionKey": encryption_key,
                "linkedItems": linked_items,
                "checking": checking,
                "creditCards": credit_cards,
                "smtp": config.get("smtp", {}),
                "thresholds": config.get("thresholds", {"low_balance_warning": 5000}),
                "schedule": config.get("schedule", {
                    "sync": {"days": "mon,wed,sat", "hour": 10, "minute": 0},
                    "digest": {"days": "sat", "hour": 10, "minute": 5},
                }),
                "digest": config.get("digest", {
                    "projection_days_detail": 30,
                    "projection_days_lowpoint": 30,
                }),
                "recurring": fe_recurring,
            },
        })
    except Exception as e:
        log.exception("Failed to load existing config")
        return jsonify({"ok": False, "error": str(e)}), 500


# ── Plaid credentials ────────────────────────────────────────────────

@app.route("/api/validate-plaid", methods=["POST"])
def validate_plaid():
    data = request.json
    client_id = data.get("client_id", "").strip()
    secret = data.get("secret", "").strip()
    environment = data.get("environment", "sandbox")

    if not client_id or not secret:
        return jsonify({"ok": False, "error": "Client ID and secret are required."}), 400

    config_stub = {
        "plaid": {
            "client_id": client_id,
            "secret": secret,
            "environment": environment,
        }
    }

    try:
        client = plaid_client.create_client(config_stub)
        # Quick validation: creating a link token will fail if credentials are bad.
        plaid_client.create_link_token(client, "setup_test")
    except Exception as e:
        msg = str(e)
        if hasattr(e, "body"):
            try:
                body = json.loads(e.body)
                msg = body.get("error_message", msg)
            except Exception:
                pass
        return jsonify({"ok": False, "error": msg}), 400

    _state["plaid_config"] = config_stub["plaid"]
    _state["plaid_client"] = client
    return jsonify({"ok": True})


# ── Encryption key ───────────────────────────────────────────────────

@app.route("/api/generate-key", methods=["POST"])
def generate_key():
    key = Fernet.generate_key().decode()
    _state["encryption_key"] = key
    os.environ["PLAID_ENCRYPTION_KEY"] = key
    return jsonify({"ok": True, "key": key})


# ── Session restore (after page reload) ──────────────────────────────

@app.route("/api/restore-session", methods=["POST"])
def restore_session():
    """Re-establish server-side state from saved frontend state."""
    data = request.json
    plaid_creds = data.get("plaid", {})
    encryption_key = data.get("encryption_key", "")

    client_id = plaid_creds.get("client_id", "").strip()
    secret = plaid_creds.get("secret", "").strip()
    environment = plaid_creds.get("environment", "sandbox")

    if not client_id or not secret:
        return jsonify({"ok": False, "error": "Missing Plaid credentials."}), 400

    config_stub = {
        "plaid": {
            "client_id": client_id,
            "secret": secret,
            "environment": environment,
        }
    }

    try:
        client = plaid_client.create_client(config_stub)
        _state["plaid_config"] = config_stub["plaid"]
        _state["plaid_client"] = client
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400

    if encryption_key:
        _state["encryption_key"] = encryption_key
        os.environ["PLAID_ENCRYPTION_KEY"] = encryption_key

    return jsonify({"ok": True})


# ── Plaid Link flow ─────────────────────────────────────────────────

@app.route("/api/create-link-token", methods=["POST"])
def create_link_token():
    data = request.json
    item_alias = data.get("item_alias", "").strip()
    if not item_alias:
        return jsonify({"ok": False, "error": "Item alias is required."}), 400
    if not _state["plaid_client"]:
        return jsonify({"ok": False, "error": "Validate Plaid credentials first."}), 400

    try:
        token = plaid_client.create_link_token(_state["plaid_client"], item_alias)
        return jsonify({"ok": True, "link_token": token})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/exchange-token", methods=["POST"])
def exchange_token():
    data = request.json
    item_alias = data.get("item_alias", "").strip()
    public_token = data.get("public_token", "").strip()

    if not item_alias or not public_token:
        return jsonify({"ok": False, "error": "Item alias and public token are required."}), 400

    client = _state["plaid_client"]
    data_dir = _state["data_dir"]

    try:
        access_token = plaid_client.exchange_public_token(client, public_token)
        os.makedirs(data_dir, exist_ok=True)
        # During setup, a stale token file encrypted with a previous key may
        # exist (e.g. user refreshed and restarted the wizard). Remove it so
        # save_token doesn't fail trying to decrypt with the new key.
        try:
            plaid_client.load_tokens(data_dir)
        except SystemExit:
            token_path = plaid_client._resolve_writable_path(data_dir)
            if os.path.exists(token_path):
                os.remove(token_path)
        plaid_client.save_token(data_dir, item_alias, access_token)

        accounts = plaid_client.list_accounts(client, access_token)
        account_list = []
        for acct in accounts:
            account_list.append({
                "name": acct.name or "",
                "official_name": acct.official_name or "",
                "type": str(acct.type) if acct.type else "",
                "subtype": str(acct.subtype) if acct.subtype else "",
                "mask": acct.mask or "",
            })

        return jsonify({"ok": True, "accounts": account_list})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


# ── Accounts ─────────────────────────────────────────────────────────

@app.route("/api/accounts")
def list_accounts():
    """List all accounts across all linked Plaid items."""
    client = _state["plaid_client"]
    data_dir = _state["data_dir"]

    if not client:
        return jsonify({"ok": False, "error": "Validate Plaid credentials first."}), 400

    tokens = plaid_client.load_tokens(data_dir)
    if not tokens:
        return jsonify({"ok": True, "items": {}})

    items = {}
    for item_alias, access_token in tokens.items():
        try:
            accounts = plaid_client.list_accounts(client, access_token)
            items[item_alias] = [
                {
                    "name": acct.name or "",
                    "official_name": acct.official_name or "",
                    "type": str(acct.type) if acct.type else "",
                    "subtype": str(acct.subtype) if acct.subtype else "",
                    "mask": acct.mask or "",
                }
                for acct in accounts
            ]
        except Exception as e:
            items[item_alias] = {"error": str(e)}

    return jsonify({"ok": True, "items": items})


# ── Recurring transaction suggestions ────────────────────────────────

@app.route("/api/suggest-recurring", methods=["POST"])
def suggest_recurring():
    """Fetch recent checking transactions from Plaid and suggest recurring ones."""
    import re
    from collections import defaultdict
    from datetime import date, timedelta

    from plaid.model.transactions_get_request import TransactionsGetRequest
    from plaid.model.transactions_get_request_options import TransactionsGetRequestOptions

    data = request.json
    checking = data.get("checking", {})
    plaid_item = checking.get("plaid_item", "")
    account_name = checking.get("account_name", "")
    account_mask = checking.get("account_mask", "")

    if not plaid_item or not account_name:
        return jsonify({"ok": False, "error": "Checking account not configured."}), 400

    client = _state["plaid_client"]
    data_dir = _state["data_dir"]
    if not client:
        return jsonify({"ok": False, "error": "Plaid client not initialized."}), 400

    tokens = plaid_client.load_tokens(data_dir)
    access_token = tokens.get(plaid_item)
    if not access_token:
        return jsonify({"ok": False, "error": f"No token for item '{plaid_item}'."}), 400

    try:
        # Resolve checking account ID
        from plaid.model.accounts_get_request import AccountsGetRequest
        acct_resp = client.accounts_get(AccountsGetRequest(access_token=access_token))
        checking_account_id = None
        for acct in acct_resp.accounts:
            name = f"{acct.name or ''} {acct.official_name or ''}".lower()
            if account_name.lower() in name and str(acct.type) == "depository":
                if account_mask and (acct.mask or "") != account_mask:
                    continue
                checking_account_id = acct.account_id
                break

        if not checking_account_id:
            return jsonify({"ok": False, "error": "Could not find checking account."}), 400

        # Fetch 90 days of transactions
        end_date = date.today()
        start_date = end_date - timedelta(days=90)
        txn_resp = client.transactions_get(TransactionsGetRequest(
            access_token=access_token,
            start_date=start_date,
            end_date=end_date,
            options=TransactionsGetRequestOptions(
                account_ids=[checking_account_id],
                count=500,
            ),
        ))
        transactions = txn_resp.transactions

        # Fetch additional pages if needed
        while len(transactions) < txn_resp.total_transactions:
            txn_resp = client.transactions_get(TransactionsGetRequest(
                access_token=access_token,
                start_date=start_date,
                end_date=end_date,
                options=TransactionsGetRequestOptions(
                    account_ids=[checking_account_id],
                    count=500,
                    offset=len(transactions),
                ),
            ))
            transactions.extend(txn_resp.transactions)

        # Normalize and group by name
        def normalize(name):
            # Strip trailing numbers, dates, reference IDs
            name = name.upper().strip()
            name = re.sub(r'\d{2,}[-/]\d{2,}([-/]\d{2,})?', '', name)  # dates
            name = re.sub(r'#\S+', '', name)       # reference numbers
            name = re.sub(r'\s{2,}', ' ', name)     # collapse whitespace
            return name.strip()

        groups = defaultdict(list)
        for txn in transactions:
            if txn.pending:
                continue
            key = normalize(txn.name)
            groups[key].append({
                "date": str(txn.date),
                "name": txn.name,
                "amount": -txn.amount,  # Plaid: positive = debit
            })

        # Analyze groups for recurring patterns
        suggestions = []
        for key, txns in groups.items():
            if len(txns) < 2:
                continue

            amounts = [t["amount"] for t in txns]
            dates = sorted([t["date"] for t in txns])
            avg_amount = sum(abs(a) for a in amounts) / len(amounts)
            is_credit = all(a > 0 for a in amounts)
            is_debit = all(a < 0 for a in amounts)
            if not is_credit and not is_debit:
                continue  # mixed sign — skip

            # Compute intervals between occurrences
            date_objs = [date.fromisoformat(d) for d in dates]
            intervals = [(date_objs[i+1] - date_objs[i]).days for i in range(len(date_objs)-1)]
            if not intervals:
                continue
            avg_interval = sum(intervals) / len(intervals)

            # Determine likely frequency
            frequency = None
            day = None
            anchor_date = None
            if 25 <= avg_interval <= 35:
                frequency = "monthly"
                days_of_month = [d.day for d in date_objs]
                day = round(sum(days_of_month) / len(days_of_month))
            elif 12 <= avg_interval <= 16:
                frequency = "biweekly"
                anchor_date = dates[-1]  # most recent as anchor
            elif 80 <= avg_interval <= 100:
                frequency = "quarterly"
                days_of_month = [d.day for d in date_objs]
                day = round(sum(days_of_month) / len(days_of_month))
            else:
                continue  # irregular — skip

            # Amount: fixed or range?
            amount_min = min(abs(a) for a in amounts)
            amount_max = max(abs(a) for a in amounts)
            use_range = (amount_max - amount_min) > 5.0

            suggestion = {
                "name": txns[0]["name"],
                "normalized_name": key,
                "type": "credit" if is_credit else "debit",
                "frequency": frequency,
                "occurrences": len(txns),
                "avg_amount": round(avg_amount, 2),
                "match_name": key,
            }

            if use_range:
                suggestion["amount_range"] = [round(amount_min, 2), round(amount_max, 2)]
            else:
                suggestion["amount"] = round(avg_amount, 2)

            if frequency == "monthly" or frequency == "quarterly":
                suggestion["day"] = day
            if frequency == "biweekly":
                suggestion["anchor_date"] = anchor_date
            if frequency == "quarterly":
                suggestion["anchor_month"] = date_objs[-1].month

            suggestions.append(suggestion)

        # Sort by occurrence count descending
        suggestions.sort(key=lambda s: s["occurrences"], reverse=True)

        return jsonify({"ok": True, "suggestions": suggestions})
    except Exception as e:
        log.exception("Failed to suggest recurring transactions")
        return jsonify({"ok": False, "error": str(e)}), 500


# ── Email test ───────────────────────────────────────────────────────

@app.route("/api/test-email", methods=["POST"])
def test_email():
    data = request.json
    host = data.get("host", "").strip()
    port = int(data.get("port", 465))
    username = data.get("username", "").strip()
    password = data.get("password", "").strip()
    from_addr = data.get("from", "").strip()
    to_addr = data.get("to", "").strip()

    if not all([host, port, username, password, from_addr, to_addr]):
        return jsonify({"ok": False, "error": "All SMTP fields are required."}), 400

    msg = MIMEText("This is a test email from Checking Projections setup wizard.")
    msg["Subject"] = "Checking Projections - Test Email"
    msg["From"] = from_addr
    msg["To"] = to_addr

    try:
        if port == 465:
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(host, port, context=context, timeout=10) as server:
                server.login(username, password)
                server.sendmail(from_addr, [to_addr], msg.as_string())
        else:
            with smtplib.SMTP(host, port, timeout=10) as server:
                server.starttls()
                server.login(username, password)
                server.sendmail(from_addr, [to_addr], msg.as_string())
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


# ── Save config ──────────────────────────────────────────────────────

@app.route("/api/save", methods=["POST"])
def save_config():
    data = request.json
    config = data.get("config")
    recurring = data.get("recurring")

    if not config:
        return jsonify({"ok": False, "error": "Config data is required."}), 400

    try:
        # Extract Plaid secrets before writing config — these go to .env only
        plaid_client_id = config.get("plaid", {}).pop("client_id", "")
        plaid_secret = config.get("plaid", {}).pop("secret", "")

        # Write config.yaml (without Plaid secrets)
        config_path = BASE_DIR / "config.yaml"
        with open(config_path, "w") as f:
            yaml.dump(config, f, default_flow_style=False, sort_keys=False, allow_unicode=True)

        # Write recurring.yaml
        recurring_path = BASE_DIR / "recurring.yaml"
        recurring_data = {"transactions": recurring or []}
        with open(recurring_path, "w") as f:
            yaml.dump(recurring_data, f, default_flow_style=False, sort_keys=False, allow_unicode=True)

        # Write .env with all secrets
        env_path = BASE_DIR / ".env"
        env_lines = []
        if env_path.exists():
            env_lines = env_path.read_text().splitlines()

        env_vars = {
            "PLAID_CLIENT_ID": plaid_client_id,
            "PLAID_SECRET": plaid_secret,
            "PLAID_ENCRYPTION_KEY": _state.get("encryption_key", ""),
        }
        for var_name, var_value in env_vars.items():
            if not var_value:
                continue
            found = False
            for i, line in enumerate(env_lines):
                if line.startswith(f"{var_name}="):
                    env_lines[i] = f"{var_name}={var_value}"
                    found = True
                    break
            if not found:
                env_lines.append(f"{var_name}={var_value}")

        env_path.write_text("\n".join(env_lines) + "\n")

        # Initialize database
        data_dir = config.get("data_dir", "./data")
        os.makedirs(os.path.join(str(BASE_DIR), data_dir.lstrip("./")), exist_ok=True)
        conn = db.get_db(data_dir)
        conn.close()

        return jsonify({"ok": True})
    except Exception as e:
        log.exception("Failed to save config")
        return jsonify({"ok": False, "error": str(e)}), 500


# ── Shutdown ─────────────────────────────────────────────────────────

@app.route("/api/shutdown", methods=["POST"])
def shutdown():
    """Shut down the setup wizard server after setup is complete."""
    import threading
    def _shutdown():
        import time
        time.sleep(1)
        os._exit(0)
    threading.Thread(target=_shutdown, daemon=True).start()
    return jsonify({"ok": True})


# ── Launch ───────────────────────────────────────────────────────────

def run_setup_wizard():
    """Start the setup wizard server and open the browser."""
    # Bind to 0.0.0.0 so the wizard is accessible when running inside Docker.
    # Detect Docker by checking for /.dockerenv or /app working directory.
    in_docker = os.path.exists("/.dockerenv") or os.getcwd() == "/app"
    host = "0.0.0.0" if in_docker else "127.0.0.1"

    print(f"Starting setup wizard at http://localhost:{PORT}")
    print("Complete the setup in your browser. Press Ctrl+C to stop.\n")
    if not in_docker:
        webbrowser.open(f"http://localhost:{PORT}")
    app.run(host=host, port=PORT, debug=False)
