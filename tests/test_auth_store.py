import json
import os
import stat

from ccweb.auth import AuthError, read_access_token
from ccweb.config import load_config
from ccweb.store import Store
import pytest


def test_reads_token_from_credentials_file(tmp_path, monkeypatch):
    monkeypatch.delenv("CCWEB_ACCESS_TOKEN", raising=False)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr("sys.platform", "linux")
    (tmp_path / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": "tok-123", "expiresAt": 1}}))
    assert read_access_token(load_config()) == "tok-123"


def test_missing_login_message_has_no_secret(tmp_path, monkeypatch):
    monkeypatch.delenv("CCWEB_ACCESS_TOKEN", raising=False)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr("sys.platform", "linux")
    with pytest.raises(AuthError, match="claude auth login"):
        read_access_token(load_config())


def test_store_is_private(tmp_path):
    store = Store(tmp_path / "ccweb" / "state.json")
    store.add_known("session_01Abcdefghijkl", title="t")
    store.set_checkout("owner/repo", "/tmp")
    mode = stat.S_IMODE(os.stat(store.path).st_mode)
    assert mode == 0o600
    again = Store(store.path)
    assert again.known["session_01Abcdefghijkl"]["title"] == "t"
    assert again.checkout_for("owner/repo") == "/tmp"


def test_layout_choices_are_kept(tmp_path):
    store = Store(tmp_path / "state.json")
    assert store.ui == {}
    store.set_ui(side_width=42.5)
    assert Store(store.path).ui == {"side_width": 42.5}
