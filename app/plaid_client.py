import json
import logging
import os

import plaid
from cryptography.fernet import Fernet, InvalidToken
from plaid.api import plaid_api
from plaid.model.country_code import CountryCode
from plaid.model.item_public_token_exchange_request import ItemPublicTokenExchangeRequest
from plaid.model.link_token_create_request import LinkTokenCreateRequest
from plaid.model.link_token_create_request_user import LinkTokenCreateRequestUser
from plaid.model.products import Products
from plaid.model.sandbox_public_token_create_request import SandboxPublicTokenCreateRequest

log = logging.getLogger(__name__)


PLAID_ENVS = {
    "sandbox": plaid.Environment.Sandbox,
    "development": getattr(plaid.Environment, "Development", plaid.Environment.Sandbox),
    "production": plaid.Environment.Production,
}


def create_client(config):
    """Create a Plaid API client from config."""
    plaid_config = config["plaid"]
    configuration = plaid.Configuration(
        host=PLAID_ENVS[plaid_config["environment"]],
        api_key={
            "clientId": plaid_config["client_id"],
            "secret": plaid_config["secret"],
        },
    )
    api_client = plaid.ApiClient(configuration)
    return plaid_api.PlaidApi(api_client)


def create_link_token(client, item_alias):
    """Generate a link token for initial account linking."""
    request = LinkTokenCreateRequest(
        user=LinkTokenCreateRequestUser(client_user_id=f"user-{item_alias}"),
        client_name="Checking Projections",
        products=[Products("transactions"), Products("liabilities")],
        country_codes=[CountryCode("US")],
        language="en",
    )
    response = client.link_token_create(request)
    return response.link_token


def exchange_public_token(client, public_token):
    """Exchange a public token for an access token."""
    request = ItemPublicTokenExchangeRequest(public_token=public_token)
    response = client.item_public_token_exchange(request)
    return response.access_token


def sandbox_create_token(client, institution_id, products=None):
    """Create a sandbox public token for testing (bypasses Link UI)."""
    if products is None:
        products = [Products("transactions"), Products("liabilities")]
    request = SandboxPublicTokenCreateRequest(
        institution_id=institution_id,
        initial_products=products,
    )
    response = client.sandbox_public_token_create(request)
    return response.public_token


def list_accounts(client, access_token):
    """List all accounts for an access token."""
    from plaid.model.accounts_get_request import AccountsGetRequest
    request = AccountsGetRequest(access_token=access_token)
    response = client.accounts_get(request)
    return response.accounts


DOCKER_SECRET_PATH = "/run/secrets/plaid_tokens"
HOST_SECRETS_PATH = os.path.join(os.path.dirname(__file__), "..", "secrets", "plaid_tokens.json")


def _get_fernet():
    """Return a Fernet instance if PLAID_ENCRYPTION_KEY is set, else None."""
    key = os.environ.get("PLAID_ENCRYPTION_KEY")
    if not key:
        return None
    return Fernet(key.encode())


def _resolve_tokens_path(data_dir):
    """Determine which token file to read, in priority order."""
    env_path = os.environ.get("PLAID_TOKENS_FILE")
    if env_path and os.path.exists(env_path):
        return env_path
    if os.path.exists(DOCKER_SECRET_PATH):
        return DOCKER_SECRET_PATH
    if os.path.exists(HOST_SECRETS_PATH):
        return HOST_SECRETS_PATH
    return os.path.join(data_dir, "plaid_tokens.json")


def _resolve_writable_path(data_dir):
    """Determine a writable path for saving tokens (never /run/secrets/)."""
    env_path = os.environ.get("PLAID_TOKENS_FILE")
    if env_path:
        return env_path
    if os.path.exists(os.path.dirname(HOST_SECRETS_PATH)):
        return HOST_SECRETS_PATH
    return os.path.join(data_dir, "plaid_tokens.json")


def load_tokens(data_dir):
    """Load saved access tokens. Decrypts if PLAID_ENCRYPTION_KEY is set."""
    path = _resolve_tokens_path(data_dir)
    if not os.path.exists(path):
        return {}

    with open(path, "rb") as f:
        raw = f.read()

    fernet = _get_fernet()
    if fernet:
        try:
            decrypted = fernet.decrypt(raw)
            return json.loads(decrypted)
        except InvalidToken:
            raise SystemExit(
                "Failed to decrypt token file. Check PLAID_ENCRYPTION_KEY or "
                "run 'encrypt-tokens' to migrate plaintext tokens."
            )
    else:
        return json.loads(raw)


def save_token(data_dir, item_alias, access_token):
    """Save an access token. Encrypts if PLAID_ENCRYPTION_KEY is set."""
    tokens = load_tokens(data_dir)
    tokens[item_alias] = access_token
    _write_tokens(data_dir, tokens)


def _write_tokens(data_dir, tokens):
    """Write the tokens dict to disk, encrypting if key is available."""
    path = _resolve_writable_path(data_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)

    payload = json.dumps(tokens, indent=2).encode()
    fernet = _get_fernet()
    if fernet:
        payload = fernet.encrypt(payload)

    with open(path, "wb") as f:
        f.write(payload)
    os.chmod(path, 0o600)


def encrypt_existing_tokens(data_dir):
    """Migrate a plaintext token file to encrypted format in-place."""
    fernet = _get_fernet()
    if not fernet:
        raise SystemExit("Set PLAID_ENCRYPTION_KEY before running encrypt-tokens.")

    path = _resolve_writable_path(data_dir)
    if not os.path.exists(path):
        raise SystemExit(f"No token file found at {path}")

    with open(path, "rb") as f:
        raw = f.read()

    # Check if already encrypted
    try:
        fernet.decrypt(raw)
        print("Token file is already encrypted.")
        return
    except InvalidToken:
        pass

    # Parse as plaintext JSON, then re-write encrypted
    tokens = json.loads(raw)
    _write_tokens(data_dir, tokens)
    log.info("Encrypted token file at %s", path)
