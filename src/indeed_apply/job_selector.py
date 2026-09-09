"""Produce a short list of relevant, Indeed-Apply-able postings.

Loads Indeed search results in the restored authenticated context, reads the
posting cards, and keeps only postings that:

* use Indeed's in-platform apply ("Easily apply" / Indeed Apply) — external-ATS
  postings are logged and dropped, this module can't complete them;
* are not excluded by ``job_preferences.keywords_exclude``;
* do not already have an application row (dedup against ``storage``).

If Indeed shows a bot wall / CAPTCHA, raises :class:`ManualActionRequired` and
creates no rows. Exercised manually against live Indeed; kept thin.
"""

from __future__ import annotations

import urllib.parse
from dataclasses import dataclass

from . import config
from .errors import ManualActionRequired
from .profile import Profile
from .session_manager import detect_block
from .storage import Storage

_CARD = "div.job_seen_beacon, div.cardOutline, [data-testid='slider_item']"
_EASILY_APPLY = (
    "[data-testid='indeedApply']",
    "text=/easily apply/i",
    "text=/apply now/i",
)


@dataclass(frozen=True)
class JobPosting:
    job_key: str
    title: str
    company: str
    location: str
    url: str
    indeed_apply: bool


def _search_url(query: str, location: str) -> str:
    params = {"q": query}
    if location:
        params["l"] = location
    return f"{config.INDEED_BASE_URL}/jobs?" + urllib.parse.urlencode(params)


async def _raise_if_challenge(page, response=None) -> None:
    reason = await detect_block(page, response)
    if reason:
        raise ManualActionRequired(f"job search blocked — {reason}")


async def select_jobs(
    context,
    profile: Profile,
    storage: Storage,
    *,
    query: str | None = None,
    location: str | None = None,
    limit: int | None = None,
) -> list[JobPosting]:
    """Return up to ``limit`` fresh, Indeed-Apply postings for the profile."""
    prefs = profile.job_preferences
    queries = [query] if query else list(prefs.titles)
    loc = location if location is not None else prefs.location
    cap = limit or prefs.limit

    excludes = [k.lower() for k in prefs.keywords_exclude]
    picked: dict[str, JobPosting] = {}
    dropped_external: list[str] = []

    page = await context.new_page()
    try:
        for q in queries:
            if len(picked) >= cap:
                break
            resp = await page.goto(_search_url(q, loc), wait_until="domcontentloaded")
            await _raise_if_challenge(page, resp)
            await page.wait_for_timeout(1500)  # let the job cards hydrate

            cards = page.locator(_CARD)
            for i in range(await cards.count()):
                if len(picked) >= cap:
                    break
                card = cards.nth(i)
                posting = await _read_card(card)
                if posting is None or posting.job_key in picked:
                    continue
                blob = f"{posting.title} {posting.company}".lower()
                if any(x in blob for x in excludes):
                    continue
                if storage.has_job(posting.job_key):
                    continue
                if not posting.indeed_apply:
                    dropped_external.append(f"{posting.title} @ {posting.company}")
                    continue
                picked[posting.job_key] = posting
    finally:
        await page.close()

    for d in dropped_external:
        print(f"[skip] external-ATS posting (cannot auto-apply): {d}")

    return list(picked.values())


async def _read_card(card) -> JobPosting | None:
    try:
        job_key = await card.get_attribute("data-jk")
        if not job_key:
            handle = card.locator("[data-jk]").first
            job_key = await handle.get_attribute("data-jk")
        if not job_key:
            return None

        title_loc = card.locator("h2.jobTitle a, a.jcs-JobTitle, h2 a").first
        title = (await title_loc.inner_text()).strip() if await title_loc.count() else ""

        company_loc = card.locator("[data-testid='company-name']").first
        company = (
            (await company_loc.inner_text()).strip()
            if await company_loc.count()
            else ""
        )

        location_loc = card.locator("[data-testid='text-location']").first
        location = (
            (await location_loc.inner_text()).strip()
            if await location_loc.count()
            else ""
        )

        indeed_apply = False
        for sel in _EASILY_APPLY:
            if await card.locator(sel).count():
                indeed_apply = True
                break

        url = f"{config.INDEED_BASE_URL}/viewjob?jk={job_key}"
        return JobPosting(
            job_key=job_key,
            title=title,
            company=company,
            location=location,
            url=url,
            indeed_apply=indeed_apply,
        )
    except Exception:
        return None
