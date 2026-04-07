import json
import os
import stat

import pytest
from cryptography.fernet import Fernet

from app.plaid_client import load_tokens, save_token, encrypt_existing_tokens, _resolve_tokens_path


@pytest.fixture
def secrets_dir(tmp_path):
    d = tmp_path / "secrets"
    d.mkdir()
    return d


@pytest.fixture
def data_dir(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    return d


class TestTokenResolution:
    def test_env_var_takes_priority(self, tmp_path, monkeypatch):
        env_file = tmp_path / "env_tokens.json"
        env_file.write_text("{}")
        monkeypatch.setenv("PLAID_TOKENS_FILE", str(env_file))
        assert _resolve_tokens_path(str(tmp_path / "data")) == str(env_file)

    def test_falls_back_to_data_dir(self, data_dir, monkeypatch):
        monkeypatch.delenv("PLAID_TOKENS_FILE", raising=False)
        # No Docker secret, no host secrets dir → data_dir fallback
        path = _resolve_tokens_path(str(data_dir))
        assert path == os.path.join(str(data_dir), "plaid_tokens.json")


class TestLoadSaveTokens:
    def test_load_empty(self, data_dir, monkeypatch):
        monkeypatch.delenv("PLAID_TOKENS_FILE", raising=False)
        monkeypatch.delenv("PLAID_ENCRYPTION_KEY", raising=False)
        tokens = load_tokens(str(data_dir))
        assert tokens == {}

    def test_save_and_load_plaintext(self, data_dir, monkeypatch):
        monkeypatch.delenv("PLAID_ENCRYPTION_KEY", raising=False)
        monkeypatch.setenv("PLAID_TOKENS_FILE", str(data_dir / "tokens.json"))

        save_token(str(data_dir), "cap1", "access-token-123")
        tokens = load_tokens(str(data_dir))
        assert tokens == {"cap1": "access-token-123"}

    def test_save_and_load_encrypted(self, data_dir, monkeypatch):
        key = Fernet.generate_key().decode()
        monkeypatch.setenv("PLAID_ENCRYPTION_KEY", key)
        monkeypatch.setenv("PLAID_TOKENS_FILE", str(data_dir / "tokens.json"))

        save_token(str(data_dir), "cap1", "access-token-secret")
        tokens = load_tokens(str(data_dir))
        assert tokens == {"cap1": "access-token-secret"}

        # Verify the file on disk is not plaintext
        raw = (data_dir / "tokens.json").read_bytes()
        assert b"access-token-secret" not in raw

    def test_file_permissions(self, data_dir, monkeypatch):
        monkeypatch.delenv("PLAID_ENCRYPTION_KEY", raising=False)
        monkeypatch.setenv("PLAID_TOKENS_FILE", str(data_dir / "tokens.json"))

        save_token(str(data_dir), "cap1", "tok")
        mode = os.stat(data_dir / "tokens.json").st_mode
        assert stat.S_IMODE(mode) == 0o600

    def test_save_preserves_existing_tokens(self, data_dir, monkeypatch):
        monkeypatch.delenv("PLAID_ENCRYPTION_KEY", raising=False)
        monkeypatch.setenv("PLAID_TOKENS_FILE", str(data_dir / "tokens.json"))

        save_token(str(data_dir), "cap1", "tok1")
        save_token(str(data_dir), "chase1", "tok2")
        tokens = load_tokens(str(data_dir))
        assert tokens == {"cap1": "tok1", "chase1": "tok2"}

    def test_wrong_key_fails(self, data_dir, monkeypatch):
        key1 = Fernet.generate_key().decode()
        key2 = Fernet.generate_key().decode()
        monkeypatch.setenv("PLAID_TOKENS_FILE", str(data_dir / "tokens.json"))

        monkeypatch.setenv("PLAID_ENCRYPTION_KEY", key1)
        save_token(str(data_dir), "cap1", "secret")

        monkeypatch.setenv("PLAID_ENCRYPTION_KEY", key2)
        with pytest.raises(SystemExit, match="Failed to decrypt"):
            load_tokens(str(data_dir))


class TestEncryptExistingTokens:
    def test_encrypts_plaintext_file(self, data_dir, monkeypatch):
        token_path = data_dir / "tokens.json"
        token_path.write_text(json.dumps({"cap1": "tok1"}))
        monkeypatch.setenv("PLAID_TOKENS_FILE", str(token_path))

        key = Fernet.generate_key().decode()
        monkeypatch.setenv("PLAID_ENCRYPTION_KEY", key)

        encrypt_existing_tokens(str(data_dir))

        # File should now be encrypted
        raw = token_path.read_bytes()
        assert b"tok1" not in raw

        # But load_tokens should still work
        tokens = load_tokens(str(data_dir))
        assert tokens == {"cap1": "tok1"}

    def test_already_encrypted_is_noop(self, data_dir, monkeypatch, capsys):
        key = Fernet.generate_key().decode()
        monkeypatch.setenv("PLAID_ENCRYPTION_KEY", key)
        monkeypatch.setenv("PLAID_TOKENS_FILE", str(data_dir / "tokens.json"))

        save_token(str(data_dir), "cap1", "tok1")
        encrypt_existing_tokens(str(data_dir))
        output = capsys.readouterr().out
        assert "already encrypted" in output

    def test_no_key_exits(self, data_dir, monkeypatch):
        monkeypatch.delenv("PLAID_ENCRYPTION_KEY", raising=False)
        with pytest.raises(SystemExit, match="Set PLAID_ENCRYPTION_KEY"):
            encrypt_existing_tokens(str(data_dir))

    def test_no_file_exits(self, data_dir, monkeypatch):
        key = Fernet.generate_key().decode()
        monkeypatch.setenv("PLAID_ENCRYPTION_KEY", key)
        monkeypatch.setenv("PLAID_TOKENS_FILE", str(data_dir / "nonexistent.json"))
        with pytest.raises(SystemExit, match="No token file found"):
            encrypt_existing_tokens(str(data_dir))
