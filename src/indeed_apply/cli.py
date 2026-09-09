"""Minimal Typer CLI that wires the modules together.

Defaults are safe: headed browser, and **no application is submitted** unless
``--confirm`` is passed. Every command that can hit Indeed catches the module's
typed errors and prints a short, actionable line instead of a traceback.
"""

from __future__ import annotations

import asyncio
import os

import typer

from . import config, session_manager
from .errors import IndeedApplyError, ManualActionRequired
from .job_selector import JobPosting, select_jobs
from .profile import load as load_profile
from .state_machine import Status, is_terminal
from .storage import Storage

app = typer.Typer(
    add_completion=False,
    help="Minimal end-to-end Indeed auto-apply workflow.",
    no_args_is_help=True,
)

_BROWSER_OPT = typer.Option(
    None,
    "--browser",
    help="Drive a real browser: 'chrome' or 'msedge' (default: bundled Chromium). "
    "Try this if Indeed's Cloudflare check loops. Same as INDEED_BROWSER_CHANNEL.",
)


def _use_browser(channel: str | None) -> None:
    """Route --browser into the env var every launch helper reads."""
    if channel:
        os.environ["INDEED_BROWSER_CHANNEL"] = channel


def _storage() -> Storage:
    s = Storage()
    s.init_db()
    return s


def _job_from_row(row) -> JobPosting:
    return JobPosting(
        job_key=row.job_key,
        title=row.title,
        company=row.company,
        location=row.location,
        url=row.url or f"{config.INDEED_BASE_URL}/viewjob?jk={row.job_key}",
        indeed_apply=True,
    )


# --- session ------------------------------------------------------------------------


@app.command()
def keygen() -> None:
    """Create .secrets/session.key if it does not exist."""
    path = session_manager.keygen()
    typer.echo(f"session key ready: {path}")


@app.command()
def login(browser: str = _BROWSER_OPT) -> None:
    """Headed browser: log in to Indeed manually, then the session is captured.

    (Equivalent to ``capture-session`` — the two are merged so capture happens
    while the authenticated browser is still open.)
    """
    _use_browser(browser)
    _capture()


@app.command("capture-session")
def capture_session(browser: str = _BROWSER_OPT) -> None:
    """Open a headed browser, wait for manual login, store the encrypted session."""
    _use_browser(browser)
    _capture()


def _capture() -> None:
    try:
        asyncio.run(session_manager.capture())
    except IndeedApplyError as exc:
        typer.echo(f"error: {exc}")
        raise typer.Exit(1)


@app.command("session-status")
def session_status(
    check: bool = typer.Option(
        False, "--check", help="Launch a headless browser and probe Indeed for validity."
    ),
    browser: str = _BROWSER_OPT,
) -> None:
    """Report whether a captured session is present, its age, and (optionally) validity."""
    _use_browser(browser)
    if not session_manager.is_present():
        typer.echo("session: MISSING — run `login`")
        raise typer.Exit(1)
    secs = session_manager.age() or 0
    typer.echo(f"session: present, age {secs / 3600:.1f}h ({config.session_enc_path()})")
    if not check:
        typer.echo("validity: not checked (pass --check)")
        return
    try:
        ok, reason = asyncio.run(_probe_validity())
    except IndeedApplyError as exc:
        typer.echo(f"validity: MANUAL ACTION — {exc}")
        raise typer.Exit(2)
    typer.echo(f"validity: {'OK' if ok else 'EXPIRED'} — {reason}")
    raise typer.Exit(0 if ok else 3)


async def _probe_validity():
    from playwright.async_api import async_playwright

    from .session_manager import check_validity, launch_chromium, restore

    async with async_playwright() as pw:
        browser = await launch_chromium(pw, headless=True)
        try:
            context = await browser.new_context(storage_state=restore())
            return await check_validity(context)
        finally:
            await browser.close()


@app.command()
def unblock(browser: str = _BROWSER_OPT) -> None:
    """Open a visible browser so YOU can clear an Indeed anti-bot wall.

    Use when another command reports a 'Request Blocked' / anti-bot wall. A
    browser window opens with your restored session; complete any 'verify you
    are human' check yourself (or wait / reload for a hard block), then press
    ENTER. The cleared session is re-saved so later commands reuse it. This
    never solves a challenge for you.

    If the bundled Chromium loops on the check, retry with ``--browser chrome``.
    """
    _use_browser(browser)
    try:
        asyncio.run(session_manager.manual_unblock())
    except IndeedApplyError as exc:
        typer.echo(f"error: {exc}")
        raise typer.Exit(2)


# --- job selection ----------------------------------------------------------------


@app.command("select-jobs")
def select_jobs_cmd(
    query: str = typer.Option(None, "--query"),
    location: str = typer.Option(None, "--location"),
    limit: int = typer.Option(None, "--limit"),
    headed: bool = typer.Option(
        False, "--headed", help="Show the browser (can help past an anti-bot block)."
    ),
    browser: str = _BROWSER_OPT,
) -> None:
    """Search Indeed with the restored session; persist matches as PENDING rows."""
    _use_browser(browser)
    storage = _storage()
    profile = load_profile()
    try:
        postings = asyncio.run(
            _run_select(storage, profile, query, location, limit, headed)
        )
    except ManualActionRequired as exc:
        typer.echo(f"blocked: {exc}")
        typer.echo(
            "Indeed's anti-bot wall is up. This module will not bypass it. Run "
            "`python -m indeed_apply unblock` to clear it yourself in a visible "
            "browser, then retry — or wait / switch network."
        )
        raise typer.Exit(2)
    except IndeedApplyError as exc:
        typer.echo(f"error: {exc}")
        raise typer.Exit(2)

    if not postings:
        typer.echo("no new Indeed-Apply postings found")
        return
    for p in postings:
        row = storage.upsert_job(
            p.job_key,
            title=p.title,
            company=p.company,
            location=p.location,
            url=p.url,
        )
        typer.echo(f"  [{row.id}] {p.title} @ {p.company} — {p.location}  ({p.job_key})")
    typer.echo(f"{len(postings)} posting(s) queued as PENDING")


async def _run_select(storage, profile, query, location, limit, headed=False):
    from playwright.async_api import async_playwright

    from .session_manager import check_validity, launch_chromium, restore

    async with async_playwright() as pw:
        browser = await launch_chromium(pw, headless=not headed)
        try:
            context = await browser.new_context(storage_state=restore())
            ok, reason = await check_validity(context)
            if not ok:
                typer.echo(f"session invalid ({reason}) — re-run login + capture-session")
                raise typer.Exit(3)
            return await select_jobs(
                context, profile, storage, query=query, location=location, limit=limit
            )
        finally:
            await browser.close()


# --- apply ----------------------------------------------------------------------------


@app.command("apply-all")
def apply_all(
    confirm: bool = typer.Option(False, "--confirm", help="Actually submit applications."),
    headless: bool = typer.Option(False, "--headless"),
    browser: str = _BROWSER_OPT,
) -> None:
    """Run the apply flow for every PENDING application row."""
    _use_browser(browser)
    storage = _storage()
    profile = load_profile()
    pending = storage.list(Status.PENDING)
    if not pending:
        typer.echo("no PENDING applications — run `select-jobs` first")
        return
    for row in pending:
        typer.echo(f"--- applying [{row.id}] {row.title} @ {row.company}")
        asyncio.run(
            _apply_one(storage, _job_from_row(row), profile, confirm, headless)
        )


@app.command()
def apply(
    job_id: int = typer.Argument(..., help="applications.id from `status` / `select-jobs`"),
    confirm: bool = typer.Option(False, "--confirm"),
    headless: bool = typer.Option(False, "--headless"),
    browser: str = _BROWSER_OPT,
) -> None:
    """Run the apply flow for one application row."""
    _use_browser(browser)
    storage = _storage()
    row = storage.get(job_id)
    if row is None:
        typer.echo(f"no application with id {job_id}")
        raise typer.Exit(1)
    if is_terminal(row.status):
        typer.echo(f"application {job_id} is already {row.status}")
        raise typer.Exit(0)
    profile = load_profile()
    asyncio.run(_apply_one(storage, _job_from_row(row), profile, confirm, headless))


@app.command()
def resume(
    job_id: int = typer.Argument(...),
    confirm: bool = typer.Option(False, "--confirm"),
    headless: bool = typer.Option(False, "--headless"),
    browser: str = _BROWSER_OPT,
) -> None:
    """Resume a MANUAL_ACTION_REQUIRED application. Idempotent."""
    from . import apply_runner

    _use_browser(browser)
    storage = _storage()
    try:
        result = asyncio.run(
            apply_runner.resume(
                job_id, storage=storage, confirm=confirm, headless=headless
            )
        )
    except (IndeedApplyError, KeyError) as exc:
        typer.echo(f"error: {exc}")
        raise typer.Exit(1)
    typer.echo(f"application {job_id}: {result.status}")


async def _apply_one(storage, job, profile, confirm, headless):
    from . import apply_runner

    try:
        result = await apply_runner.apply(
            job, profile, storage=storage, confirm=confirm, headless=headless
        )
        typer.echo(f"    -> {result.status}"
                   + (f" ({result.manual_reason})" if result.manual_reason else ""))
    except IndeedApplyError as exc:
        typer.echo(f"    -> error: {exc}")


# --- status ------------------------------------------------------------------------


@app.command()
def status(
    id: int = typer.Option(None, "--id", help="Show one application + its full history."),
    all: bool = typer.Option(False, "--all", help="Show every application."),
) -> None:
    """Show applications and their status history."""
    storage = _storage()
    if id is not None:
        row = storage.get(id)
        if row is None:
            typer.echo(f"no application with id {id}")
            raise typer.Exit(1)
        _print_row(row)
        typer.echo("  history:")
        for h in storage.history(id):
            frm = h.from_status or "-"
            typer.echo(f"    {h.created_at}  {frm} -> {h.to_status}"
                       + (f"  ({h.reason})" if h.reason else ""))
        return

    rows = storage.list()
    if not rows:
        typer.echo("no applications yet")
        return
    for row in rows:
        _print_row(row)
    if not all:
        typer.echo("(use --id N for full history)")


def _print_row(row) -> None:
    line = f"[{row.id}] {row.status:<22} {row.title} @ {row.company}"
    if row.manual_reason:
        line += f"  << {row.manual_reason}"
    typer.echo(line)


if __name__ == "__main__":  # pragma: no cover
    app()
