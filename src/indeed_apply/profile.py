"""Load and validate the candidate profile from ``profile.json``.

The profile is the only source of personal data the module uses. Parsing is
strict and fails fast: a missing required field or a missing resume file raises
:class:`~indeed_apply.errors.ProfileError` with a message that names exactly
what is wrong, so the operator can fix the JSON and re-run.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import profile_path as default_profile_path
from .errors import ProfileError


@dataclass(frozen=True)
class Contact:
    name: str
    email: str
    phone: str
    location: str


@dataclass(frozen=True)
class WorkExperience:
    title: str
    company: str
    start: str = ""
    end: str = ""
    description: str = ""


@dataclass(frozen=True)
class Education:
    school: str
    degree: str = ""
    field_of_study: str = ""
    start: str = ""
    end: str = ""


@dataclass(frozen=True)
class JobPreferences:
    titles: tuple[str, ...]
    location: str = ""
    remote: bool = False
    min_salary: int | None = None
    keywords_exclude: tuple[str, ...] = ()
    limit: int = 5


@dataclass(frozen=True)
class Profile:
    contact: Contact
    resume_path: Path
    work_experience: tuple[WorkExperience, ...]
    education: tuple[Education, ...]
    answers: dict[str, str]
    job_preferences: JobPreferences

    def lookup_answer(self, label: str) -> str | None:
        """Return a stored answer for ``label`` by exact normalized key match.

        Normalization = lowercase, strip, collapse internal whitespace, drop
        surrounding punctuation. This is intentionally *not* fuzzy: callers
        that need alias handling layer it on top, and anything short of a clean
        match is meant to fall through to a human (see CLAUDE.md §5.4).
        """
        target = _normalize_label(label)
        for key, value in self.answers.items():
            if _normalize_label(key) == target:
                return value
        return None


def _normalize_label(s: str) -> str:
    s = s.strip().lower()
    s = "".join(ch for ch in s if ch.isalnum() or ch.isspace())
    return " ".join(s.split())


# -- loading -----------------------------------------------------------------------


def load(path: str | Path | None = None) -> Profile:
    """Parse and validate the profile JSON at ``path`` (default: config).

    Raises :class:`ProfileError` on a missing file, invalid JSON, a missing
    required field, or a ``resume_path`` that does not point at a real file.
    """
    p = Path(path) if path is not None else default_profile_path()
    if not p.exists():
        raise ProfileError(f"profile not found: {p}")
    try:
        raw: Any = json.loads(p.read_text())
    except json.JSONDecodeError as exc:
        raise ProfileError(f"profile is not valid JSON ({p}): {exc}") from exc
    if not isinstance(raw, dict):
        raise ProfileError(f"profile root must be a JSON object ({p})")

    contact = _parse_contact(_require(raw, "contact", dict))

    resume_raw = _require(raw, "resume_path", str)
    resume_path = Path(resume_raw)
    if not resume_path.is_absolute():
        # Resolve relative to the profile file's directory for portability.
        resume_path = (p.parent / resume_path).resolve()
    if not resume_path.is_file():
        raise ProfileError(f"resume_path does not point at a file: {resume_path}")

    work = tuple(
        _parse_work(item, i)
        for i, item in enumerate(raw.get("work_experience", []) or [])
    )
    education = tuple(
        _parse_education(item, i)
        for i, item in enumerate(raw.get("education", []) or [])
    )

    answers_raw = raw.get("answers", {}) or {}
    if not isinstance(answers_raw, dict):
        raise ProfileError("'answers' must be a JSON object of label -> answer")
    answers = {str(k): _scalar_to_str(v) for k, v in answers_raw.items()}

    prefs = _parse_preferences(_require(raw, "job_preferences", dict))

    return Profile(
        contact=contact,
        resume_path=resume_path,
        work_experience=work,
        education=education,
        answers=answers,
        job_preferences=prefs,
    )


# -- field parsers ---------------------------------------------------------------


def _require(obj: dict, key: str, typ: type) -> Any:
    if key not in obj or obj[key] is None:
        raise ProfileError(f"missing required field: {key!r}")
    value = obj[key]
    if not isinstance(value, typ):
        raise ProfileError(
            f"field {key!r} must be {typ.__name__}, got {type(value).__name__}"
        )
    return value


def _opt_str(obj: dict, *keys: str) -> str:
    """First present, non-empty string among ``keys`` (accepts field-name aliases)."""
    for k in keys:
        v = obj.get(k)
        if v is not None and str(v).strip():
            return str(v).strip()
    return ""


def _require_str(obj: dict, key: str, *aliases: str, context: str) -> str:
    value = _opt_str(obj, key, *aliases)
    if not value:
        shown = " / ".join((key, *aliases))
        raise ProfileError(f"{context}: missing required string field {shown!r}")
    return value


def _parse_contact(obj: dict) -> Contact:
    return Contact(
        name=_require_str(obj, "name", "full_name", context="contact"),
        email=_require_str(obj, "email", context="contact"),
        phone=_opt_str(obj, "phone"),
        location=_opt_str(obj, "location"),
    )


def _parse_work(obj: Any, idx: int) -> WorkExperience:
    if not isinstance(obj, dict):
        raise ProfileError(f"work_experience[{idx}] must be an object")
    ctx = f"work_experience[{idx}]"
    return WorkExperience(
        title=_require_str(obj, "title", "role", context=ctx),
        company=_require_str(obj, "company", "employer", context=ctx),
        start=_opt_str(obj, "start", "start_date"),
        end=_opt_str(obj, "end", "end_date"),
        description=_opt_str(obj, "description", "summary"),
    )


def _parse_education(obj: Any, idx: int) -> Education:
    if not isinstance(obj, dict):
        raise ProfileError(f"education[{idx}] must be an object")
    ctx = f"education[{idx}]"
    return Education(
        school=_require_str(obj, "school", "institution", context=ctx),
        degree=_opt_str(obj, "degree"),
        field_of_study=_opt_str(obj, "field_of_study", "field", "concentration", "major"),
        start=_opt_str(obj, "start", "start_date"),
        end=_opt_str(obj, "end", "end_date"),
    )


def _parse_preferences(obj: dict) -> JobPreferences:
    titles_raw = obj.get("titles")
    if not isinstance(titles_raw, list) or not titles_raw:
        raise ProfileError("job_preferences.titles must be a non-empty list")
    titles = tuple(str(t).strip() for t in titles_raw if str(t).strip())
    if not titles:
        raise ProfileError("job_preferences.titles has no usable entries")

    min_salary = obj.get("min_salary")
    if min_salary is None:
        min_salary = obj.get("min_salary_annual")
    if min_salary is not None and not isinstance(min_salary, (int, float)):
        raise ProfileError("job_preferences.min_salary must be a number or null")

    limit = obj.get("limit", 5)
    if not isinstance(limit, int) or limit < 1:
        raise ProfileError("job_preferences.limit must be a positive integer")

    excludes = obj.get("keywords_exclude", []) or []
    if not isinstance(excludes, list):
        raise ProfileError("job_preferences.keywords_exclude must be a list")

    return JobPreferences(
        titles=titles,
        location=_opt_str(obj, "location"),
        remote=_coerce_remote(obj.get("remote", False)),
        min_salary=int(min_salary) if min_salary is not None else None,
        keywords_exclude=tuple(str(k).strip() for k in excludes if str(k).strip()),
        limit=limit,
    )


def _coerce_remote(v: Any) -> bool:
    """Accept a bool or a loose string ('any', 'remote', 'onsite', ...)."""
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    return s in {"any", "yes", "true", "remote", "hybrid", "either"}


def _scalar_to_str(v: Any) -> str:
    if isinstance(v, bool):
        return "Yes" if v else "No"
    return str(v)
