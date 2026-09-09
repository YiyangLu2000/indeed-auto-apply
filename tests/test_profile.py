"""Profile: the committed example loads, and validation fails fast on bad input."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from indeed_apply import profile as profile_mod
from indeed_apply.errors import ProfileError

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "profile.example.json"


def test_example_profile_loads_and_validates():
    p = profile_mod.load(EXAMPLE)
    assert p.contact.name == "Jane Placeholder"
    assert p.contact.email == "jane.placeholder@example.com"
    assert p.resume_path.is_file()
    assert p.job_preferences.titles[0] == "Backend Engineer"
    assert p.job_preferences.limit == 5
    assert p.job_preferences.remote is True
    assert len(p.work_experience) == 2
    assert p.work_experience[0].company == "Example Corp"
    assert len(p.education) == 1


def test_lookup_answer_is_exact_normalized_not_fuzzy():
    p = profile_mod.load(EXAMPLE)
    # exact-but-for-case/punctuation/whitespace still matches
    assert (
        p.lookup_answer("  are you authorized to work in the united states  ")
        == "Yes"
    )
    # a partial / different question does not
    assert p.lookup_answer("work authorization") is None
    assert p.lookup_answer("authorized to work") is None


def _write(tmp_path: Path, obj: dict) -> Path:
    f = tmp_path / "profile.json"
    f.write_text(json.dumps(obj))
    return f


@pytest.fixture()
def base(tmp_path):
    (tmp_path / "r.txt").write_text("resume")
    return {
        "contact": {"name": "A B", "email": "a@b.com", "phone": "", "location": ""},
        "resume_path": "r.txt",
        "job_preferences": {"titles": ["Engineer"]},
    }


def test_missing_file(tmp_path):
    with pytest.raises(ProfileError, match="not found"):
        profile_mod.load(tmp_path / "nope.json")


def test_invalid_json(tmp_path):
    f = tmp_path / "profile.json"
    f.write_text("{not json")
    with pytest.raises(ProfileError, match="not valid JSON"):
        profile_mod.load(f)


def test_missing_contact(tmp_path, base):
    del base["contact"]
    with pytest.raises(ProfileError, match="contact"):
        profile_mod.load(_write(tmp_path, base))


def test_missing_contact_name(tmp_path, base):
    base["contact"]["name"] = "   "
    with pytest.raises(ProfileError, match="name"):
        profile_mod.load(_write(tmp_path, base))


def test_resume_must_exist(tmp_path, base):
    base["resume_path"] = "does-not-exist.pdf"
    with pytest.raises(ProfileError, match="resume_path"):
        profile_mod.load(_write(tmp_path, base))


def test_titles_required_non_empty(tmp_path, base):
    base["job_preferences"]["titles"] = []
    with pytest.raises(ProfileError, match="titles"):
        profile_mod.load(_write(tmp_path, base))


def test_limit_must_be_positive_int(tmp_path, base):
    base["job_preferences"]["titles"] = ["Engineer"]
    base["job_preferences"]["limit"] = 0
    with pytest.raises(ProfileError, match="limit"):
        profile_mod.load(_write(tmp_path, base))


def test_bool_answer_coerced_to_yes_no(tmp_path, base):
    base["answers"] = {"Sponsorship?": False, "Authorized?": True}
    p = profile_mod.load(_write(tmp_path, base))
    assert p.answers["Sponsorship?"] == "No"
    assert p.answers["Authorized?"] == "Yes"


def test_minimal_profile_ok(tmp_path, base):
    p = profile_mod.load(_write(tmp_path, base))
    assert p.job_preferences.limit == 5
    assert p.work_experience == ()
    assert p.education == ()
    assert p.answers == {}
