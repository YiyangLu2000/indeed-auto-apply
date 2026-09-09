"""Environment + filesystem path resolution.

Every configurable path or secret location is resolved here so the rest of the
module never reads ``os.environ`` directly. Paths are returned as
:class:`pathlib.Path`; nothing here creates files except :func:`ensure_dirs`,
which only makes directories.
"""

from __future__ import annotations

import os
from pathlib import Path

# Repo root = two levels up from this file (src/indeed_apply/config.py).
REPO_ROOT = Path(__file__).resolve().parents[2]

# Indeed surfaces used by session_manager / job_selector / apply_runner.
INDEED_BASE_URL = "https://www.indeed.com"
INDEED_LOGIN_HOST = "secure.indeed.com"


def _path(env_var: str, default_rel: str) -> Path:
    """Resolve ``env_var`` to an absolute Path, falling back to a repo-relative default."""
    raw = os.environ.get(env_var)
    if raw:
        return Path(raw).expanduser().resolve()
    return (REPO_ROOT / default_rel).resolve()


def db_path() -> Path:
    """SQLite database file. Override with ``INDEED_DB_PATH``."""
    return _path("INDEED_DB_PATH", "data/applications.db")


def session_enc_path() -> Path:
    """Encrypted ``storage_state`` blob. Override with ``INDEED_SESSION_ENC``."""
    return _path("INDEED_SESSION_ENC", ".secrets/session.enc")


def session_key_file() -> Path:
    """Default Fernet key file. Override with ``INDEED_SESSION_KEY_FILE``."""
    return _path("INDEED_SESSION_KEY_FILE", ".secrets/session.key")


def profile_path() -> Path:
    """Candidate profile JSON. Override with ``INDEED_PROFILE_PATH``."""
    return _path("INDEED_PROFILE_PATH", "profile.json")


def runs_dir() -> Path:
    """Directory for per-job screenshots + DOM snapshots on stop."""
    return _path("INDEED_RUNS_DIR", "runs")


def session_key_env() -> str | None:
    """Raw urlsafe-base64 Fernet key from ``INDEED_SESSION_KEY``, if set."""
    return os.environ.get("INDEED_SESSION_KEY") or None


def browser_channel() -> str | None:
    """Playwright browser channel to drive: ``chrome`` / ``msedge`` / ``None``.

    ``None`` (default) uses Playwright's bundled Chromium. Set
    ``INDEED_BROWSER_CHANNEL=chrome`` to drive the real installed Google Chrome
    instead — often enough to clear a Cloudflare managed challenge that the
    bundled build loops on. This is the actual browser, not a stealth patch.
    """
    return os.environ.get("INDEED_BROWSER_CHANNEL") or None


def cdp_url() -> str:
    """DevTools endpoint for ``--attach`` mode. Override with ``INDEED_CDP_URL``."""
    return os.environ.get("INDEED_CDP_URL") or "http://localhost:9222"


def chrome_bin() -> str | None:
    """Explicit Chrome executable for ``chrome-debug`` (``INDEED_CHROME_BIN``)."""
    return os.environ.get("INDEED_CHROME_BIN") or None


def chrome_attach_profile_dir() -> Path:
    """Dedicated profile dir for the ``chrome-debug`` browser. git-ignored."""
    return _path("INDEED_CHROME_PROFILE_DIR", ".secrets/chrome-attach-profile")


def ensure_dirs() -> None:
    """Create the writable directories the module expects. Safe to call repeatedly."""
    for p in (db_path().parent, session_enc_path().parent, runs_dir()):
        p.mkdir(parents=True, exist_ok=True)
