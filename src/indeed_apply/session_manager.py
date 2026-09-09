"""Capture and restore the Indeed login session, encrypted at rest.

No browser is kept running. ``capture()`` opens a headed Chromium, waits for a
human to finish logging in, then serialises ``context.storage_state()``,
Fernet-encrypts it, and writes ``.secrets/session.enc``. ``restore()`` decrypts
that blob back to an in-memory dict for ``browser.new_context(storage_state=...)``
— plaintext never touches disk.

``check_validity()`` is the concrete "still logged in vs expired" probe run
against a restored context before any real work (see CLAUDE.md §5.1).

This module is exercised manually against live Indeed; keep it thin.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from . import config
from .errors import ConfigError, ManualActionRequired, SessionExpired, SessionMissing

# --- Signals used by check_validity() -------------------------------------------

#: URL fragments that mean "Indeed is asking us to log in again".
_LOGIN_URL_MARKERS = (
    "secure.indeed.com/auth",
    "secure.indeed.com/account/login",
    "/account/login",
    "/auth?",
    "/auth/",
)

#: URL / text fragments that mean "anti-bot or CAPTCHA wall" (not a plain expiry).
_CHALLENGE_URL_MARKERS = ("/blocked", "challenge", "hcaptcha", "px-captcha")
_CHALLENGE_TEXT_MARKERS = (
    "additional verification required",
    "verify you are a human",
    "unusual traffic",
    "enable javascript and cookies to continue",
)

#: Cookies Indeed sets only after a successful login.
_AUTH_COOKIE_PRIMARY = "CTK"
_AUTH_COOKIE_SECONDARY = ("SHOE", "PPID", "SURF")

#: DOM markers on indeed.com/ that distinguish logged-in from logged-out.
_LOGGED_IN_SELECTORS = (
    '[data-gnav-element-name="AccountMenu"]',
    "#AccountMenu",
    'a[href*="/account"][data-gnav-element-name]',
)
_LOGGED_OUT_SELECTORS = (
    'a[data-gnav-element-name="SignIn"]',
    'a[href*="/account/login"]',
)


# --- Key resolution / encryption ---------------------------------------------------


def _resolve_key() -> bytes:
    """Resolve the Fernet key: env raw -> env file -> default key file."""
    raw = config.session_key_env()
    if raw:
        return raw.encode()
    key_file = config.session_key_file()
    env_file = None
    import os

    if os.environ.get("INDEED_SESSION_KEY_FILE"):
        env_file = Path(os.environ["INDEED_SESSION_KEY_FILE"]).expanduser()
    path = env_file or key_file
    if not path.is_file():
        raise ConfigError(
            f"no session key: set INDEED_SESSION_KEY, or run `keygen` to create {path}"
        )
    return path.read_text().strip().encode()


def _fernet() -> Fernet:
    try:
        return Fernet(_resolve_key())
    except (ValueError, TypeError) as exc:
        raise ConfigError(f"session key is not a valid Fernet key: {exc}") from exc


def keygen(*, force: bool = False) -> Path:
    """Create the default key file if missing. Returns its path.

    Never overwrites an existing key unless ``force`` (that would orphan any
    session encrypted with the old key).
    """
    path = config.session_key_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file() and not force:
        return path
    path.write_bytes(Fernet.generate_key())
    path.chmod(0o600)
    return path


# --- Presence / age --------------------------------------------------------------


def is_present() -> bool:
    return config.session_enc_path().is_file()


def age() -> float | None:
    """Seconds since the encrypted session blob was last written, or ``None``."""
    p = config.session_enc_path()
    if not p.is_file():
        return None
    return time.time() - p.stat().st_mtime


# --- Capture / restore -----------------------------------------------------------


async def capture(*, indeed_url: str | None = None, poll_seconds: float = 2.0) -> Path:
    """Headed login capture. Blocks until a logged-in signal appears, then stores.

    Returns the path of the encrypted blob. Requires a display (headed Chromium)
    and ``playwright install chromium`` to have been run.
    """
    from playwright.async_api import async_playwright

    config.ensure_dirs()
    url = indeed_url or config.INDEED_BASE_URL
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto(url, wait_until="domcontentloaded")
        print(
            "A browser window is open. Log in to Indeed manually "
            "(email + emailed passcode, dismiss any passkey prompt).\n"
            "Waiting for a logged-in signal..."
        )
        try:
            while True:
                ok, reason = await _inspect_logged_in(context, page)
                if ok:
                    break
                await page.wait_for_timeout(int(poll_seconds * 1000))
        except KeyboardInterrupt:  # pragma: no cover - operator abort
            await browser.close()
            raise

        state = await context.storage_state()
        blob = _fernet().encrypt(json.dumps(state).encode())
        out = config.session_enc_path()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(blob)
        out.chmod(0o600)
        await browser.close()
    print(f"Session captured and encrypted -> {out}")
    return out


def restore() -> dict:
    """Decrypt the stored session to an in-memory ``storage_state`` dict."""
    p = config.session_enc_path()
    if not p.is_file():
        raise SessionMissing(f"no captured session at {p}; run `capture-session`")
    try:
        plaintext = _fernet().decrypt(p.read_bytes())
    except InvalidToken as exc:
        raise ConfigError(
            "could not decrypt session.enc — wrong key? "
            "(INDEED_SESSION_KEY / INDEED_SESSION_KEY_FILE / .secrets/session.key)"
        ) from exc
    return json.loads(plaintext)


# --- Validity probe (CLAUDE.md §5.1) --------------------------------------------


async def check_validity(context) -> tuple[bool, str]:
    """Read-only probe: is this restored context still logged in to Indeed?

    Returns ``(True, "ok")`` only when the homepage shows a logged-in DOM
    marker. Returns ``(False, reason)`` for a plain expiry / logged-out state
    (caller: tell the human to re-run ``login`` + ``capture-session``). Raises
    :class:`ManualActionRequired` if Indeed shows an anti-bot / CAPTCHA wall
    here — that needs a human in a headed browser, not just a fresh capture.

    Never interacts with a challenge; only navigates and inspects.
    """
    page = await context.new_page()
    try:
        await page.goto(config.INDEED_BASE_URL + "/", wait_until="domcontentloaded")
        landing = page.url.lower()

        # 1. Challenge wall? -> not a normal expiry.
        if any(m in landing for m in _CHALLENGE_URL_MARKERS):
            raise ManualActionRequired(f"anti-bot wall on session check: {page.url}")
        try:
            body_text = (await page.inner_text("body"))[:4000].lower()
        except Exception:
            body_text = ""
        if any(m in body_text for m in _CHALLENGE_TEXT_MARKERS):
            raise ManualActionRequired(
                "anti-bot / verification wall on session check"
            )

        # 2. Login URL redirect?
        if any(m in landing for m in _LOGIN_URL_MARKERS):
            return False, f"redirected to login ({page.url})"

        # 3. Cookie signal.
        cookies = {c["name"]: c for c in await context.cookies()}
        now = time.time()

        def _live(name: str) -> bool:
            c = cookies.get(name)
            if not c:
                return False
            exp = c.get("expires", -1)
            return exp in (-1, 0) or exp > now

        has_primary = _live(_AUTH_COOKIE_PRIMARY)
        has_secondary = any(_live(n) for n in _AUTH_COOKIE_SECONDARY)
        cookie_ok = has_primary and has_secondary

        # 4. DOM signal — authoritative.
        dom_logged_out = await _any_visible(page, _LOGGED_OUT_SELECTORS)
        dom_logged_in = await _any_visible(page, _LOGGED_IN_SELECTORS)

        if dom_logged_out and not dom_logged_in:
            return False, "Indeed shows a signed-out homepage (session stale)"
        if not dom_logged_in:
            if not cookie_ok:
                return False, "no live auth cookie and no logged-in marker"
            return False, "auth cookie present but no logged-in marker on homepage"

        # 5. Corroborate on a profile-gated page.
        try:
            await page.goto(
                "https://myjobs.indeed.com/applied", wait_until="domcontentloaded"
            )
            if any(m in page.url.lower() for m in _LOGIN_URL_MARKERS):
                return False, "profile page redirected to login"
        except Exception:
            pass  # corroboration only; homepage marker already said logged-in

        return True, "ok"
    finally:
        await page.close()


async def _any_visible(page, selectors) -> bool:
    for sel in selectors:
        try:
            if await page.locator(sel).first.is_visible(timeout=2500):
                return True
        except Exception:
            continue
    return False


async def _inspect_logged_in(context, page) -> tuple[bool, str]:
    """Lightweight check used while polling during capture()."""
    try:
        if await _any_visible(page, _LOGGED_IN_SELECTORS):
            return True, "account menu visible"
        cookies = {c["name"] for c in await context.cookies()}
        if _AUTH_COOKIE_PRIMARY in cookies and (
            cookies & set(_AUTH_COOKIE_SECONDARY)
        ):
            return True, "auth cookies set"
    except Exception:
        pass
    return False, "not yet"


def require_valid_or_raise(ok: bool, reason: str) -> None:
    """Helper for callers: turn a ``check_validity`` failure into ``SessionExpired``."""
    if not ok:
        raise SessionExpired(reason)
