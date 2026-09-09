"""Conservative screening-answer matcher (CLAUDE.md §5.4) — no Indeed involved.

Only high-confidence matches on closed control types auto-answer; everything
else must raise ManualActionRequired naming the question.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from indeed_apply import profile as profile_mod
from indeed_apply.apply_runner import Question, resolve_answer
from indeed_apply.errors import ManualActionRequired

EXAMPLE = Path(__file__).resolve().parents[1] / "profile.example.json"


@pytest.fixture(scope="module")
def profile():
    return profile_mod.load(EXAMPLE)


# -- auto-answered: high-confidence closed controls -------------------------------


def test_boolean_exact_label(profile):
    q = Question(
        "Are you authorized to work in the United States?", "boolean", ("Yes", "No")
    )
    assert resolve_answer(q, profile) == "Yes"


def test_boolean_via_small_alias_table(profile):
    q = Question(
        "Are you legally authorized to work in the US?", "boolean", ("Yes", "No")
    )
    assert resolve_answer(q, profile) == "No" or resolve_answer(q, profile) == "Yes"
    # the aliased key resolves to the profile's stored "Yes"
    assert resolve_answer(q, profile) == "Yes"


def test_sponsorship_alias_resolves_to_no(profile):
    q = Question("Do you require visa sponsorship?", "boolean", ("Yes", "No"))
    assert resolve_answer(q, profile) == "No"


def test_number_question(profile):
    q = Question(
        "How many years of professional software engineering experience do you have?",
        "number",
    )
    assert resolve_answer(q, profile) == "3"


def test_single_select_exact_option(profile):
    q = Question(
        "What is your notice period?",
        "single_select",
        ("Immediately", "2 weeks", "1 month", "More than 1 month"),
    )
    assert resolve_answer(q, profile) == "2 weeks"


# -- paused: everything not high-confidence -------------------------------------------


def test_free_text_always_pauses(profile):
    q = Question("Why do you want to work here?", "text")
    with pytest.raises(ManualActionRequired, match="Why do you want to work here"):
        resolve_answer(q, profile)


def test_textarea_pauses(profile):
    q = Question("Describe a hard bug you fixed.", "textarea")
    with pytest.raises(ManualActionRequired):
        resolve_answer(q, profile)


def test_multi_select_pauses(profile):
    q = Question("Which languages do you know?", "multi_select")
    with pytest.raises(ManualActionRequired):
        resolve_answer(q, profile)


def test_unmapped_label_pauses(profile):
    q = Question("Do you have a security clearance?", "boolean", ("Yes", "No"))
    with pytest.raises(ManualActionRequired, match="no confident answer"):
        resolve_answer(q, profile)


def test_partial_label_match_pauses(profile):
    # substring of a known key must NOT match
    q = Question("authorized to work", "boolean", ("Yes", "No"))
    with pytest.raises(ManualActionRequired):
        resolve_answer(q, profile)


def test_value_not_matching_any_option_pauses(profile):
    q = Question(
        "What is your notice period?",
        "single_select",
        ("Immediately", "30 days", "60 days"),  # no "2 weeks"
    )
    with pytest.raises(ManualActionRequired, match="exactly one option"):
        resolve_answer(q, profile)


def test_unknown_kind_pauses(profile):
    q = Question("Anything?", "unknown")
    with pytest.raises(ManualActionRequired):
        resolve_answer(q, profile)


def test_numeric_question_with_nonnumeric_profile_value_pauses(profile):
    q = Question("What is your notice period?", "number")  # profile value "2 weeks"
    assert resolve_answer(q, profile) == "2"  # extracts leading integer
    q2 = Question("Are you willing to relocate?", "number")  # value "Yes"
    with pytest.raises(ManualActionRequired):
        resolve_answer(q2, profile)
