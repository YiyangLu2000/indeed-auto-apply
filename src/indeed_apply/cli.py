"""Minimal Typer CLI that wires the modules together.

Defaults are safe: headed browser, and **no application is submitted** unless
``--confirm`` is passed. Every command that can hit Indeed catches the module's
typed errors and prints a short, actionable line instead of a traceback.
"""

from __future__ import annotations

import asyncio

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
def login() -> None:
    """Headed browser: log in to Indeed manually, then the session is captured.

    (Equivalent to ``capture-session`` — the two are merged so capture happens
    while the authenticated browser is still open.)
    """
    _capture()


@app.command("capture-session")
def capture_session() -> None:
    """Open a headed browser, wait for manual login, store the encrypted session."""
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
) -> None:
    """Report whether a captured session is present, its age, and (optionally) validity."""
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

    from .session_manager import check_validity, restore

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(storage_state=restore())
            return await check_validity(context)
        finally:
            await browser.close()


# --- job selection ----------------------------------------------------------------


@app.command("select-jobs")
def select_jobs_cmd(
    query: str = typer.Option(None, "--query"),
    location: str = typer.Option(None, "--location"),
    limit: int = typer.Option(None, "--limit"),
    headed: bool = typer.Option(
        False, "--headed", help="Show the browser (can help past an anti-bot block)."
    ),
) -> None:
    """Search Indeed with the restored session; persist matches as PENDING rows."""
    storage = _storage()
    profile = load_profile()
    try:
        postings = asyncio.run(
            _run_select(storage, profile, query, location, limit, headed)
        )
    except ManualActionRequired as exc:
        typer.echo(f"blocked: {exc}")
        typer.echo(
            "Indeed's anti-bot wall is up for this network. This module will not "
            "bypass it. Options: wait and retry later, try `--headed`, or run from "
            "a different network, then re-run `login` there."
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

    from .session_manager import check_validity, restore

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=not headed)
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
) -> None:
    """Run the apply flow for every PENDING application row."""
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
) -> None:
    """Run the apply flow for one application row."""
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
) -> None:
    """Resume a MANUAL_ACTION_REQUIRED application. Idempotent."""
    from . import apply_runner

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
