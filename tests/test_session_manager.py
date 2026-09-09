"""session_manager: key resolution, keygen, and the encrypt/restore round trip.

No browser here — capture() and check_validity() are exercised manually against
live Indeed (see README). This covers the "encrypted at rest" property.
"""

from __future__ import annotations

import json

import pytest
from cryptography.fernet import Fernet

from indeed_apply import config, session_manager
from indeed_apply.errors import ConfigError, SessionMissing


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "session_key_file", lambda: tmp_path / "session.key")
    monkeypatch.setattr(config, "session_enc_path", lambda: tmp_path / "session.enc")
    monkeypatch.delenv("INDEED_SESSION_KEY", raising=False)
    monkeypatch.delenv("INDEED_SESSION_KEY_FILE", raising=False)


def test_keygen_creates_key_once(tmp_path):
    p = session_manager.keygen()
    assert p.is_file()
    first = p.read_bytes()
    # a second call must not overwrite (would orphan any existing session)
    session_manager.keygen()
    assert p.read_bytes() == first
    # sanity: it's a usable Fernet key
    Fernet(first)


def test_keygen_force_rotates(tmp_path):
    p = session_manager.keygen()
    first = p.read_bytes()
    session_manager.keygen(force=True)
    assert p.read_bytes() != first


def test_restore_missing_raises(tmp_path):
    session_manager.keygen()
    with pytest.raises(SessionMissing):
        session_manager.restore()


def test_encrypt_then_restore_round_trip(tmp_path):
    session_manager.keygen()
    state = {"cookies": [{"name": "CTK", "value": "x"}], "origins": []}
    blob = session_manager._fernet().encrypt(json.dumps(state).encode())
    config.session_enc_path().write_bytes(blob)

    assert session_manager.is_present()
    assert session_manager.restore() == state


def test_restore_with_wrong_key_is_a_clean_error(tmp_path, monkeypatch):
    session_manager.keygen()
    blob = session_manager._fernet().encrypt(b"{}")
    config.session_enc_path().write_bytes(blob)
    # rotate the key so decryption must fail
    monkeypatch.setenv("INDEED_SESSION_KEY", Fernet.generate_key().decode())
    with pytest.raises(ConfigError, match="could not decrypt"):
        session_manager.restore()


def test_env_raw_key_takes_priority(tmp_path, monkeypatch):
    key = Fernet.generate_key()
    monkeypatch.setenv("INDEED_SESSION_KEY", key.decode())
    # no key file exists, but the env key should be used
    assert session_manager._resolve_key() == key


def test_age_none_when_absent(tmp_path):
    assert session_manager.age() is None
