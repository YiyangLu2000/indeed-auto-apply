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
import select
import sys
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

#: Cookies Indeed sets for *any* visitor — NOT evidence of a login.
_ANON_COOKIES = ("CTK", "SURF", "CSRF", "INDEED_CSRF_TOKEN")
#: Cookies that appear only after a successful login. ``PPID`` (persistent
#: principal id) is the most reliable; ``SHOE`` rides along with an
#: authenticated session.
_AUTH_COOKIES = ("PPID", "SHOE")

#: DOM markers on indeed.com/ that distinguish logged-in from logged-out. The
#: logout link and the "My jobs" nav item only render for an authenticated user.
_LOGGED_IN_SELECTORS = (
    '[data-gnav-element-name="AccountMenu"]',
    '[data-gnav-element-name="Profile"]',
    '[data-testid="gnav-AccountMenu"]',
    "#AccountMenu",
    'button[aria-label*="Account" i]',
    'a[href*="/account/logout"]',
    'a[href*="myjobs"]',
)
_LOGGED_OUT_SELECTORS = (
    'a[data-gnav-element-name="SignIn"]',
    'a[href*="/account/login"]',
    'a[href*="secure.indeed.com/auth"]',
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


async def capture(
    *,
    indeed_url: str | None = None,
    poll_seconds: float = 1.5,
    timeout_seconds: float = 900.0,
) -> Path:
    """Headed login capture.

    Opens a visible Chromium on Indeed, then waits until **either** a logged-in
    signal is detected (account menu / logout link in the DOM, or a post-login
    cookie) **or** the operator presses ENTER in the terminal. Then it
    serialises ``storage_state()``, encrypts it, and closes the browser.

    Requires a display (headed Chromium) and ``playwright install chromium``.
    Raises :class:`ConfigError` if the window is closed early or the wait times
    out with no login.
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
            "\nA Chromium window is open on Indeed.\n"
            "  1. Click 'Sign in' and log in (email + the emailed 6-digit code).\n"
            "  2. Dismiss any 'add a passkey' / 'save your info' prompt.\n"
            "  3. Wait until your avatar/initial shows at the top-right.\n"
            "\nCapture happens automatically once the login is detected — or press\n"
            "ENTER in this terminal to capture the current session immediately.\n"
        )
        can_prompt = sys.stdin.isatty()
        deadline = time.monotonic() + timeout_seconds
        why = "detected"
        while True:
            try:
                ok, reason = await _inspect_logged_in(context, page)
            except Exception as exc:  # window closed / navigation died
                await _safe_close(browser)
                raise ConfigError(
                    f"the login window went away before capture ({exc}); re-run `login`"
                ) from exc
            if ok:
                why = reason
                break
            if can_prompt and select.select([sys.stdin], [], [], 0)[0]:
                sys.stdin.readline()
                recheck, _ = await _inspect_logged_in(context, page)
                if recheck:
                    why = "operator-confirmed + detected"
                else:
                    why = "operator-confirmed (no login signal seen)"
                    print(
                        "! Could not confirm a logged-in state from the page. "
                        "Capturing anyway — run `session-status --check` to verify."
                    )
                break
            if time.monotonic() > deadline:
                await _safe_close(browser)
                raise ConfigError(
                    "timed out waiting for login; re-run `login` and finish the "
                    "Indeed sign-in in the opened window"
                )
            await page.wait_for_timeout(int(poll_seconds * 1000))

        print(f"Capturing session ({why})...")
        state = await context.storage_state()
        blob = _fernet().encrypt(json.dumps(state).encode())
        out = config.session_enc_path()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(blob)
        out.chmod(0o600)
        await browser.close()
    print(f"Session captured and encrypted -> {out}")
    return out


async def _safe_close(browser) -> None:
    try:
        await browser.close()
    except Exception:
        pass


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

        # 3. Cookie signal (advisory — the DOM in step 4 decides).
        cookies = {c["name"]: c for c in await context.cookies()}
        now = time.time()

        def _live(name: str) -> bool:
            c = cookies.get(name)
            if not c:
                return False
            exp = c.get("expires", -1)
            return exp in (-1, 0) or exp > now

        cookie_ok = any(_live(n) for n in _AUTH_COOKIES)

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
    """True if any selector matches a currently-visible element. Returns fast."""
    for sel in selectors:
        try:
            if await page.locator(sel).first.is_visible():
                return True
        except Exception:
            continue
    return False


async def _inspect_logged_in(context, page) -> tuple[bool, str]:
    """Lightweight logged-in check used while polling during capture().

    DOM marker is authoritative; a post-login cookie is accepted as a fallback
    for when Indeed changes its nav markup. Anonymous cookies (``CTK``,
    ``SURF``, ...) are deliberately ignored — every visitor has those.
    """
    try:
        if await _any_visible(page, _LOGGED_IN_SELECTORS):
            return True, "account menu / logout link visible"
        names = {c["name"] for c in await context.cookies()}
        hit = [n for n in _AUTH_COOKIES if n in names]
        if hit:
            return True, f"post-login cookie present ({', '.join(hit)})"
    except Exception:
        pass
    return False, "not logged in yet"


def require_valid_or_raise(ok: bool, reason: str) -> None:
    """Helper for callers: turn a ``check_validity`` failure into ``SessionExpired``."""
    if not ok:
        raise SessionExpired(reason)
