import argparse
import logging
import sys
from datetime import date

from dotenv import load_dotenv
load_dotenv()

from app.config import load_config, load_recurring
from app import db


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


def cmd_run(args):
    """Start the scheduler (default Docker entrypoint)."""
    from app.scheduler import start_scheduler
    start_scheduler()


def cmd_sync(args):
    """Run a one-shot sync."""
    from app.sync import sync_all
    config = load_config()
    recurring = load_recurring()
    conn = db.get_db(config["data_dir"])
    try:
        sync_all(config, conn, recurring)
        print("Sync complete.")
    finally:
        conn.close()


def cmd_projection(args):
    """Print the balance projection to terminal."""
    from app.projections import build_projection, find_low_points

    config = load_config()
    recurring = load_recurring()
    conn = db.get_db(config["data_dir"])

    days = args.days
    threshold = config["thresholds"]["low_balance_warning"]
    projection = build_projection(conn, config, recurring, days=days)

    balance = projection[0].opening_balance if projection else 0
    print(f"\nCurrent Checking Balance: ${balance:,.2f}\n")
    print(f"{'Date':<12} {'Transaction':<30} {'Amount':>12} {'Balance':>12}")
    print("-" * 68)

    for day_data in projection:
        if day_data.transactions:
            for txn in day_data.transactions:
                sign = "-" if txn.type == "debit" else "+"
                amt = f"{sign}${txn.amount:,.2f}"
                bal = f"${day_data.closing_balance:,.2f}"
                warn = " !!!" if day_data.closing_balance < threshold else ""
                print(f"{day_data.date.isoformat():<12} {txn.name:<30} {amt:>12} {bal:>12}{warn}")

    # Low points summary
    low_points = find_low_points(projection, threshold)
    def _lp_line(label, day):
        if day is None:
            return f"  {label}: N/A"
        return f"  {label}: {day.date.isoformat()} (${day.closing_balance:,.0f})"

    print(f"\n--- LOW BALANCE ALERTS ---")
    print(_lp_line(f"Below ${threshold:,.0f}", low_points["below_threshold"]))
    print(_lp_line("Below $0", low_points["below_zero"]))
    print(_lp_line("Low point", low_points["low_point"]))

    conn.close()


def cmd_digest(args):
    """Send the digest email now."""
    from app.digest import build_and_send_digest

    config = load_config()
    recurring = load_recurring()
    conn = db.get_db(config["data_dir"])
    try:
        build_and_send_digest(config, conn, recurring)
        print("Digest sent.")
    finally:
        conn.close()


def cmd_link(args):
    """Start the Plaid Link flow in a browser for an item."""
    from app import plaid_client
    from app.link_server import run_link_flow

    config = load_config()
    client = plaid_client.create_client(config)
    item_alias = args.item

    print(f"Creating link token for '{item_alias}'...")
    link_token = plaid_client.create_link_token(client, item_alias)

    public_token = run_link_flow(link_token)
    if not public_token:
        print("Link flow was cancelled or failed.")
        return

    access_token = plaid_client.exchange_public_token(client, public_token)
    plaid_client.save_token(config["data_dir"], item_alias, access_token)
    print(f"Access token saved for '{item_alias}'.")

    # Show accounts so user can match names to config
    accounts = plaid_client.list_accounts(client, access_token)
    print(f"\n{'Name':<30} {'Official Name':<40} {'Type':<15} {'Subtype':<15}")
    print("-" * 100)
    for acct in accounts:
        name = acct.name or ""
        official = acct.official_name or ""
        atype = str(acct.type) if acct.type else ""
        subtype = str(acct.subtype) if acct.subtype else ""
        print(f"{name:<30} {official:<40} {atype:<15} {subtype:<15}")
    print("\nUse these names in config.yaml under account_name for matching.")


def cmd_sandbox_link(args):
    """Link a sandbox institution, save the token, and list accounts."""
    from app import plaid_client

    config = load_config()
    client = plaid_client.create_client(config)
    item_alias = args.item
    institution_id = args.institution_id

    print(f"Creating sandbox token for institution '{institution_id}' as '{item_alias}'...")
    public_token = plaid_client.sandbox_create_token(client, institution_id)
    access_token = plaid_client.exchange_public_token(client, public_token)
    plaid_client.save_token(config["data_dir"], item_alias, access_token)
    print(f"Access token saved for '{item_alias}'.\n")

    accounts = plaid_client.list_accounts(client, access_token)
    print(f"{'Name':<30} {'Official Name':<40} {'Type':<15} {'Subtype':<15} {'Mask'}")
    print("-" * 110)
    for acct in accounts:
        name = acct.name or ""
        official = acct.official_name or ""
        atype = str(acct.type) if acct.type else ""
        subtype = str(acct.subtype) if acct.subtype else ""
        mask = acct.mask or ""
        print(f"{name:<30} {official:<40} {atype:<15} {subtype:<15} {mask}")


def cmd_list_accounts(args):
    """List accounts for all linked Plaid items."""
    from app import plaid_client

    config = load_config()
    client = plaid_client.create_client(config)
    tokens = plaid_client.load_tokens(config["data_dir"])

    if not tokens:
        print("No linked items. Run 'sandbox-link' or 'link' first.")
        return

    for item_alias, access_token in tokens.items():
        print(f"\n=== {item_alias} ===")
        try:
            accounts = plaid_client.list_accounts(client, access_token)
            print(f"{'Name':<30} {'Official Name':<40} {'Type':<15} {'Subtype':<15} {'Mask'}")
            print("-" * 110)
            for acct in accounts:
                name = acct.name or ""
                official = acct.official_name or ""
                atype = str(acct.type) if acct.type else ""
                subtype = str(acct.subtype) if acct.subtype else ""
                mask = acct.mask or ""
                print(f"{name:<30} {official:<40} {atype:<15} {subtype:<15} {mask}")
        except Exception as e:
            print(f"  Error: {e}")


def cmd_balances(args):
    """Show current balances for all accounts."""
    config = load_config()
    conn = db.get_db(config["data_dir"])

    balances = db.get_all_balances(conn)
    if not balances:
        print("No balance data. Run 'sync' first.")
        conn.close()
        return

    print(f"\n{'Account':<20} {'Type':<15} {'Current':>12} {'Available':>12} {'Updated'}")
    print("-" * 80)
    for row in balances:
        avail = f"${row['available_balance']:,.2f}" if row["available_balance"] is not None else "—"
        curr = f"${row['current_balance']:,.2f}" if row["current_balance"] is not None else "—"
        print(f"{row['account_id']:<20} {row['account_type']:<15} {curr:>12} {avail:>12} {row['updated_at'][:16]}")

    # Also show liabilities
    liabilities = db.get_all_liabilities(conn)
    if liabilities:
        print(f"\n{'Card':<20} {'Stmt Bal':>12} {'Min Pay':>12} {'Due Date':<12} {'Updated'}")
        print("-" * 72)
        for row in liabilities:
            stmt = f"${row['last_statement_balance']:,.2f}" if row["last_statement_balance"] is not None else "—"
            minp = f"${row['minimum_payment']:,.2f}" if row["minimum_payment"] is not None else "—"
            due = row["next_payment_due_date"] or "—"
            print(f"{row['account_id']:<20} {stmt:>12} {minp:>12} {due:<12} {row['updated_at'][:16]}")

    conn.close()


def cmd_recurring(args):
    """Show recurring transaction status."""
    config = load_config()
    recurring = load_recurring()
    conn = db.get_db(config["data_dir"])

    fulfillments = {(r["recurring_name"], r["cycle_date"]): r
                    for r in db.get_fulfillments(conn)}

    print(f"\n{'Name':<25} {'Type':<8} {'Freq':<15} {'Last Fulfilled':<20} {'Status'}")
    print("-" * 80)

    for r in recurring:
        # Find most recent fulfillment
        recent = None
        for (name, cdate), ful in fulfillments.items():
            if name == r["name"]:
                if recent is None or cdate > recent:
                    recent = cdate

        status = f"fulfilled {recent}" if recent else "no match yet"
        freq_display = r["frequency"]
        if freq_display == "monthly":
            freq_display = f"monthly (day {r['day']})"
        elif freq_display == "biweekly":
            freq_display = f"biweekly"
        elif freq_display == "twice_monthly":
            freq_display = f"2x/mo ({r['days']})"

        print(f"{r['name']:<25} {r['type']:<8} {freq_display:<15} {status}")

    conn.close()


def main():
    parser = argparse.ArgumentParser(
        prog="checking-projections",
        description="Personal checking account balance projections and tracking",
    )
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("run", help="Start scheduler (default)")
    subparsers.add_parser("sync", help="Run a one-shot sync")

    proj_parser = subparsers.add_parser("projection", help="Print balance projection")
    proj_parser.add_argument("--days", type=int, default=30, help="Number of days to project (default: 30)")

    subparsers.add_parser("digest", help="Send digest email now")

    link_parser = subparsers.add_parser("link", help="Link a Plaid item")
    link_parser.add_argument("item", help="Item alias (e.g., capital_one, chase_1)")

    subparsers.add_parser("balances", help="Show current balances")
    subparsers.add_parser("recurring", help="Show recurring transaction status")
    sandbox_parser = subparsers.add_parser("sandbox-link", help="Link a sandbox institution and list accounts")
    sandbox_parser.add_argument("item", help="Item alias to save as (e.g., capital_one, chase_1)")
    sandbox_parser.add_argument("institution_id", help="Plaid institution ID (e.g., ins_3 for Chase)")

    subparsers.add_parser("list-accounts", help="List accounts for all linked items")
    subparsers.add_parser("setup-db", help="Initialize database")
    subparsers.add_parser("generate-key", help="Generate a Fernet encryption key")
    subparsers.add_parser("encrypt-tokens", help="Encrypt existing plaintext token file")

    args = parser.parse_args()

    commands = {
        "run": cmd_run,
        "sync": cmd_sync,
        "projection": cmd_projection,
        "digest": cmd_digest,
        "link": cmd_link,
        "balances": cmd_balances,
        "recurring": cmd_recurring,
        "sandbox-link": cmd_sandbox_link,
        "list-accounts": cmd_list_accounts,
        "setup-db": lambda a: _setup_db(),
        "generate-key": lambda a: _generate_key(),
        "encrypt-tokens": lambda a: _encrypt_tokens(),
    }

    if args.command is None:
        # Default to run
        cmd_run(args)
    elif args.command in commands:
        commands[args.command](args)
    else:
        parser.print_help()


def _setup_db():
    config = load_config()
    conn = db.get_db(config["data_dir"])
    print(f"Database initialized at {config['data_dir']}/checking.db")
    conn.close()


def _generate_key():
    from cryptography.fernet import Fernet
    key = Fernet.generate_key().decode()
    print(f"Generated encryption key:\n\n  {key}\n")
    print("Add this to your environment:")
    print(f"  export PLAID_ENCRYPTION_KEY={key}")
    print("\nFor Docker, add to docker-compose.yml under environment:")
    print(f"  - PLAID_ENCRYPTION_KEY={key}")
    print("\nThen run 'encrypt-tokens' to encrypt your existing token file.")


def _encrypt_tokens():
    from app import plaid_client
    config = load_config()
    plaid_client.encrypt_existing_tokens(config["data_dir"])
    print("Token file encrypted successfully.")


if __name__ == "__main__":
    main()
