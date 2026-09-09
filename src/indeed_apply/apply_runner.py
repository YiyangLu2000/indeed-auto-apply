"""Drive Playwright through the Indeed Apply flow for one job.

Opens **one ephemeral context per run** from the restored session, walks the
Indeed Apply wizard using profile data, and asks the state machine to move the
application row between states. Every stop (manual or failed) dumps a screenshot
+ DOM snapshot to ``runs/<job_id>/``.

Screening questions are answered **conservatively** (CLAUDE.md §5.4): only a
high-confidence match on a closed control type is auto-filled; everything else
pauses with ``MANUAL_ACTION_REQUIRED`` naming the question. The matcher lives in
:func:`resolve_answer` and is unit-tested without touching Indeed.

Detection helpers only *detect* — they never touch a challenge.
Exercised manually against live Indeed; kept thin.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone

from . import config
from .errors import ManualActionRequired, SessionExpired
from .job_selector import JobPosting
from .profile import Profile
from .session_manager import check_validity, detect_block, restore
from .state_machine import Status, is_terminal, transition
from .storage import Application, Storage

_MAX_STEPS = 25  # wizard-advance safety bound


# --- Screening-question model + conservative matcher -----------------------------

# Control types we can fill unambiguously.
_CLOSED_KINDS = {"boolean", "single_select", "number"}
# Control types that always go to a human.
_OPEN_KINDS = {"text", "textarea", "multi_select", "unknown"}

# Minimal, near-exact alias set: alternate phrasings of the standard yes/no
# immigration questions. Deliberately tiny — anything not here needs an exact
# normalized key match in profile.answers.
_ALIASES: dict[str, tuple[str, ...]] = {
    "are you authorized to work in the united states": (
        "are you legally authorized to work in the united states",
        "are you legally authorized to work in the us",
        "are you authorized to work in the us",
        "do you have authorization to work in the united states",
    ),
    "will you now or in the future require sponsorship for employment visa status": (
        "will you now or in the future require sponsorship",
        "do you require visa sponsorship",
        "will you require sponsorship for employment visa status now or in the future",
    ),
}


@dataclass(frozen=True)
class Question:
    label: str
    kind: str  # boolean | single_select | number | text | textarea | multi_select | unknown
    options: tuple[str, ...] = ()
    required: bool = True


def _normalize(s: str) -> str:
    s = s.strip().lower()
    s = "".join(ch for ch in s if ch.isalnum() or ch.isspace())
    return " ".join(s.split())


def _lookup(profile: Profile, label: str) -> str | None:
    """Exact normalized key match, then the tiny alias table. No fuzzy matching."""
    direct = profile.lookup_answer(label)
    if direct is not None:
        return direct
    norm = _normalize(label)
    for canonical, aliases in _ALIASES.items():
        if norm == canonical or norm in {_normalize(a) for a in aliases}:
            val = profile.lookup_answer(canonical)
            if val is not None:
                return val
    return None


def resolve_answer(q: Question, profile: Profile) -> str:
    """Return the exact value/option text to fill, or raise ``ManualActionRequired``.

    Conservative by construction: open-ended controls, unmapped labels, and any
    value that doesn't land on exactly one offered option all raise.
    """
    if q.kind in _OPEN_KINDS:
        raise ManualActionRequired(
            f"screening question needs a human ({q.kind}): {q.label!r}"
        )
    if q.kind not in _CLOSED_KINDS:
        raise ManualActionRequired(f"unhandled question type {q.kind!r}: {q.label!r}")

    value = _lookup(profile, q.label)
    if value is None:
        raise ManualActionRequired(f"no confident answer for question: {q.label!r}")

    if q.kind == "number":
        m = re.search(r"-?\d+(\.\d+)?", value)
        if not m:
            raise ManualActionRequired(
                f"numeric question but profile value {value!r} isn't numeric: {q.label!r}"
            )
        return m.group(0)

    if q.kind == "boolean":
        v = _normalize(value)
        if v in {"yes", "y", "true"}:
            return "Yes"
        if v in {"no", "n", "false"}:
            return "No"
        raise ManualActionRequired(
            f"yes/no question but profile value {value!r} is neither: {q.label!r}"
        )

    # single_select: exactly one option must match the profile value.
    want = _normalize(value)
    matches = [opt for opt in q.options if _normalize(opt) == want]
    if len(matches) == 1:
        return matches[0]
    # allow yes/no answers to select a yes/no option
    if want in {"yes", "no"}:
        yn = [opt for opt in q.options if _normalize(opt) == want]
        if len(yn) == 1:
            return yn[0]
    raise ManualActionRequired(
        f"answer {value!r} does not map to exactly one option for: {q.label!r}"
    )


# --- Detection helpers (detect only; never interact) ---------------------------

_CAPTCHA_MARKERS_TEXT = (
    "verify you are human",
    "additional verification required",
    "unusual traffic",
    "i'm not a robot",
)
_CAPTCHA_MARKERS_URL = ("/blocked", "challenge", "hcaptcha", "px-captcha", "recaptcha")
_OTP_MARKERS_TEXT = (
    "enter the code",
    "verification code",
    "we sent a code",
    "we texted you a code",
    "check your email for a code",
    "6-digit code",
)
_LOGIN_MARKERS_URL = ("secure.indeed.com/auth", "/account/login", "/auth?")
_LOGIN_MARKERS_TEXT = ("sign in to continue", "sign in to your account")


async def _text(page) -> str:
    try:
        return (await page.inner_text("body"))[:6000].lower()
    except Exception:
        return ""


async def _looks_like_captcha(page) -> bool:
    if any(m in page.url.lower() for m in _CAPTCHA_MARKERS_URL):
        return True
    for frame_sel in ("iframe[src*='hcaptcha']", "iframe[src*='recaptcha']"):
        try:
            if await page.locator(frame_sel).count():
                return True
        except Exception:
            pass
    if any(m in await _text(page) for m in _CAPTCHA_MARKERS_TEXT):
        return True
    # Cloudflare "Request Blocked" / PerimeterX / "Just a moment" walls.
    return bool(await detect_block(page))


async def _looks_like_otp(page) -> bool:
    try:
        if await page.locator("input[autocomplete='one-time-code']").count():
            return True
    except Exception:
        pass
    return any(m in await _text(page) for m in _OTP_MARKERS_TEXT)


async def _looks_like_login_wall(page) -> bool:
    if any(m in page.url.lower() for m in _LOGIN_MARKERS_URL):
        return True
    return any(m in await _text(page) for m in _LOGIN_MARKERS_TEXT)


async def _guard(page, *, step: str) -> None:
    """Run all detectors after a navigation. Raise a specific ManualActionRequired."""
    if await _looks_like_captcha(page):
        raise ManualActionRequired(f"CAPTCHA / anti-bot wall at {step}")
    if await _looks_like_otp(page):
        raise ManualActionRequired(f"one-time passcode prompt at {step}")
    if await _looks_like_login_wall(page):
        raise ManualActionRequired(f"login wall at {step} — session likely expired")
    for txt in ("verify it's you", "passkey"):
        if txt in await _text(page):
            raise ManualActionRequired(f"{txt!r} prompt at {step}")


# --- Artifact dump -------------------------------------------------------------


async def _dump(page, job_id: int, tag: str) -> None:
    out = config.runs_dir() / str(job_id)
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = out / f"{stamp}-{tag}"
    try:
        await page.screenshot(path=str(base.with_suffix(".png")), full_page=True)
    except Exception:
        pass
    try:
        html = await page.content()
        base.with_suffix(".html").write_text(html[:500_000])
    except Exception:
        pass
    try:
        snippet = (await page.inner_text("body"))[:2000]
        base.with_suffix(".txt").write_text(f"url: {page.url}\n\n{snippet}")
    except Exception:
        pass


# --- Public API --------------------------------------------------------------------


async def apply(
    job: JobPosting,
    profile: Profile,
    *,
    storage: Storage | None = None,
    confirm: bool = False,
    headless: bool = False,
) -> Application:
    """Apply to one job. Returns the final application row for this run."""
    storage = storage or Storage()
    storage.init_db()

    app = storage.get_by_job_key(job.job_key)
    if app is None:
        app = storage.upsert_job(
            job.job_key,
            title=job.title,
            company=job.company,
            location=job.location,
            url=job.url,
        )

    if is_terminal(app.status):
        print(f"[skip] {job.job_key} already {app.status}")
        return app

    if app.status == Status.PENDING:
        app = transition(storage, app, Status.IN_PROGRESS, "apply: starting")
    elif app.status == Status.MANUAL_ACTION_REQUIRED:
        app = transition(storage, app, Status.IN_PROGRESS, "apply: retry after manual")
    # IN_PROGRESS already -> just continue

    return await _drive(storage, app, job, profile, confirm=confirm, headless=headless)


async def resume(
    job_id: int,
    *,
    storage: Storage | None = None,
    confirm: bool = False,
    headless: bool = False,
) -> Application:
    """Resume a paused application. Idempotent — safe to call again if it re-stops."""
    storage = storage or Storage()
    storage.init_db()

    app = storage.get(job_id)
    if app is None:
        raise KeyError(f"no application with id {job_id}")
    if is_terminal(app.status):
        print(f"[skip] application {job_id} is already {app.status}")
        return app
    if app.status == Status.MANUAL_ACTION_REQUIRED:
        app = transition(storage, app, Status.IN_PROGRESS, "resume")
    elif app.status != Status.IN_PROGRESS:
        raise ManualActionRequired(
            f"cannot resume from {app.status}; expected MANUAL_ACTION_REQUIRED"
        )

    job = JobPosting(
        job_key=app.job_key,
        title=app.title,
        company=app.company,
        location=app.location,
        url=app.url or f"{config.INDEED_BASE_URL}/viewjob?jk={app.job_key}",
        indeed_apply=True,
    )
    profile = _load_profile()
    return await _drive(storage, app, job, profile, confirm=confirm, headless=headless)


def _load_profile() -> Profile:
    from . import profile as profile_mod

    return profile_mod.load()


# --- Core flow ----------------------------------------------------------------------


async def _drive(
    storage: Storage,
    app: Application,
    job: JobPosting,
    profile: Profile,
    *,
    confirm: bool,
    headless: bool,
) -> Application:
    from playwright.async_api import async_playwright

    config.ensure_dirs()
    page = None
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=headless)
            context = await browser.new_context(storage_state=restore())
            try:
                ok, reason = await check_validity(context)
                if not ok:
                    raise SessionExpired(reason)

                page = await context.new_page()
                await page.goto(job.url, wait_until="domcontentloaded")
                await _guard(page, step="job posting")

                await _click_apply(page)
                await _walk_wizard(page, profile)

                # Review screen reached.
                if confirm:
                    await _submit(page)
                    result = transition(
                        storage, app, Status.SUBMITTED, "submitted and confirmed"
                    )
                    print(f"[submitted] {job.job_key}")
                    return result
                await _dump(page, app.id, "review")
                raise ManualActionRequired("awaiting human submit at review screen")
            finally:
                await browser.close()
    except ManualActionRequired as exc:
        if page is not None:
            await _safe_dump(page, app.id, "manual")
        result = transition(storage, app, Status.MANUAL_ACTION_REQUIRED, exc.reason)
        print(f"[manual] {job.job_key}: {exc.reason}")
        return result
    except SessionExpired as exc:
        result = transition(
            storage,
            app,
            Status.MANUAL_ACTION_REQUIRED,
            f"session expired: {exc.reason} — re-run login + capture-session",
        )
        print(f"[manual] {job.job_key}: session expired ({exc.reason})")
        return result
    except Exception as exc:  # genuine error -> terminal FAILED
        if page is not None:
            await _safe_dump(page, app.id, "failed")
        summary = f"{type(exc).__name__}: {exc}"
        result = transition(storage, app, Status.FAILED, summary[:500])
        print(f"[failed] {job.job_key}: {summary}")
        return result


async def _safe_dump(page, job_id: int, tag: str) -> None:
    try:
        await _dump(page, job_id, tag)
    except Exception:
        pass


async def _click_apply(page) -> None:
    for sel in (
        "#indeedApplyButton",
        "button:has-text('Apply now')",
        "[data-testid='indeedApplyButton']",
        "a:has-text('Easily apply')",
    ):
        loc = page.locator(sel).first
        try:
            if await loc.count() and await loc.is_visible():
                await loc.click()
                await page.wait_for_load_state("domcontentloaded")
                await _guard(page, step="after clicking Apply")
                return
        except Exception:
            continue
    raise RuntimeError("could not find an Indeed Apply button on the posting")


async def _walk_wizard(page, profile: Profile) -> None:
    """Advance through wizard steps until the review screen, guarding each step."""
    for _ in range(_MAX_STEPS):
        await _guard(page, step="wizard step")
        body = await _text(page)

        if _is_review(body):
            return

        if "questions from the employer" in body or "additional questions" in body:
            await _answer_screening(page, profile)

        if not await _advance(page):
            # No continue button and not review -> unknown state, let a human look.
            raise ManualActionRequired(
                "stuck on an unrecognised Indeed Apply step"
            )
    raise RuntimeError("wizard did not reach a review screen within step budget")


def _is_review(body: str) -> bool:
    return any(
        m in body
        for m in ("review your application", "please review", "submit your application")
    )


async def _advance(page) -> bool:
    for sel in (
        "button:has-text('Continue')",
        "button:has-text('Next')",
        "button:has-text('Save and continue')",
        "[data-testid='continue-button']",
    ):
        loc = page.locator(sel).first
        try:
            if await loc.count() and await loc.is_enabled():
                await loc.click()
                await page.wait_for_load_state("domcontentloaded")
                return True
        except Exception:
            continue
    return False


async def _answer_screening(page, profile: Profile) -> None:
    """Parse each question block; auto-fill only high-confidence closed controls."""
    groups = page.locator(
        "[data-testid='questions-container'] fieldset, "
        "fieldset:has(legend), div.ia-Questions-item"
    )
    count = await groups.count()
    if count == 0:
        raise ManualActionRequired(
            "employer questions present but none could be parsed"
        )
    for i in range(count):
        g = groups.nth(i)
        q = await _read_question(g)
        answer = resolve_answer(q, profile)  # raises -> pause, naming the question
        await _fill_question(g, q, answer)


async def _read_question(group) -> Question:
    label = ""
    for sel in ("legend", "label", "[data-testid='question-label']"):
        loc = group.locator(sel).first
        if await loc.count():
            label = (await loc.inner_text()).strip()
            if label:
                break

    # Determine control kind.
    if await group.locator("textarea").count():
        kind, options = "textarea", ()
    elif await group.locator("input[type='radio']").count():
        opt_loc = group.locator("label")
        options = tuple(
            (await opt_loc.nth(j).inner_text()).strip()
            for j in range(await opt_loc.count())
        )
        norm = {_normalize(o) for o in options}
        kind = "boolean" if norm <= {"yes", "no"} and norm else "single_select"
    elif await group.locator("select").count():
        opt_loc = group.locator("select option")
        options = tuple(
            (await opt_loc.nth(j).inner_text()).strip()
            for j in range(await opt_loc.count())
            if (await opt_loc.nth(j).inner_text()).strip()
        )
        kind = "single_select"
    elif await group.locator("input[type='checkbox']").count():
        kind, options = "multi_select", ()
    elif await group.locator("input[type='number']").count():
        kind, options = "number", ()
    elif await group.locator("input[type='text'], input:not([type])").count():
        kind, options = "text", ()
    else:
        kind, options = "unknown", ()

    return Question(label=label or "(unlabelled question)", kind=kind, options=options)


async def _fill_question(group, q: Question, answer: str) -> None:
    if q.kind == "number" or q.kind == "boolean" and not q.options:
        await group.locator("input").first.fill(answer)
        return
    if q.kind in ("boolean", "single_select") and q.options:
        label = group.locator("label", has_text=re.compile(rf"^\s*{re.escape(answer)}\s*$", re.I)).first
        if await label.count():
            await label.click()
            return
        # <select> case
        sel = group.locator("select").first
        if await sel.count():
            await sel.select_option(label=answer)
            return
    raise ManualActionRequired(f"could not fill answer for: {q.label!r}")


async def _submit(page) -> None:
    for sel in (
        "button:has-text('Submit your application')",
        "button:has-text('Submit application')",
        "button:has-text('Submit')",
        "[data-testid='submit-application']",
    ):
        loc = page.locator(sel).first
        if await loc.count() and await loc.is_enabled():
            await loc.click()
            await page.wait_for_load_state("domcontentloaded")
            await _guard(page, step="after submit")
            body = await _text(page)
            if any(
                m in body
                for m in (
                    "your application has been submitted",
                    "application submitted",
                    "you've applied",
                    "applied to",
                )
            ):
                return
            raise RuntimeError("clicked submit but no confirmation screen appeared")
    raise RuntimeError("could not find a Submit button on the review screen")
