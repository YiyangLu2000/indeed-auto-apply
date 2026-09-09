"""State machine: transition table, terminal states, persistence side effects."""

from __future__ import annotations

import itertools

import pytest

from indeed_apply import state_machine as sm
from indeed_apply.errors import InvalidTransition
from indeed_apply.state_machine import Status, transition
from indeed_apply.storage import Storage

LEGAL = {
    (Status.PENDING, Status.IN_PROGRESS),
    (Status.PENDING, Status.FAILED),
    (Status.IN_PROGRESS, Status.MANUAL_ACTION_REQUIRED),
    (Status.IN_PROGRESS, Status.SUBMITTED),
    (Status.IN_PROGRESS, Status.FAILED),
    (Status.MANUAL_ACTION_REQUIRED, Status.IN_PROGRESS),
    (Status.MANUAL_ACTION_REQUIRED, Status.FAILED),
}


def test_allowed_set_matches_spec():
    assert sm.ALLOWED == frozenset(LEGAL)


@pytest.mark.parametrize("frm,to", sorted(LEGAL))
def test_legal_transitions_pass(frm, to):
    assert sm.can_transition(frm, to)


@pytest.mark.parametrize(
    "frm,to",
    [p for p in itertools.product(Status, Status) if p not in LEGAL],
)
def test_illegal_transitions_rejected(frm, to):
    assert not sm.can_transition(frm, to)


def test_pending_to_failed_is_allowed():
    assert sm.can_transition("PENDING", "FAILED")


def test_terminal_states():
    assert sm.is_terminal(Status.SUBMITTED)
    assert sm.is_terminal(Status.FAILED)
    assert not sm.is_terminal(Status.PENDING)
    # nothing leaves a terminal state
    for target in Status:
        assert not sm.can_transition(Status.SUBMITTED, target)
        assert not sm.can_transition(Status.FAILED, target)


def test_unknown_status_is_not_transitionable():
    assert not sm.can_transition("BOGUS", "IN_PROGRESS")
    assert not sm.can_transition("PENDING", "NOPE")
    assert not sm.is_terminal("WHATEVER")


# -- transition() persistence -------------------------------------------------------


@pytest.fixture()
def store(tmp_path):
    s = Storage(tmp_path / "t.db")
    s.init_db()
    return s


def test_transition_persists_and_logs_history(store):
    app = store.upsert_job("jk1", title="Backend Engineer")
    app = transition(store, app, Status.IN_PROGRESS, "starting")
    assert app.status == Status.IN_PROGRESS

    app = transition(store, app, Status.MANUAL_ACTION_REQUIRED, "otp at login")
    assert app.status == Status.MANUAL_ACTION_REQUIRED
    assert app.manual_reason == "otp at login"

    app = transition(store, app, Status.IN_PROGRESS, "resumed")
    assert app.manual_reason is None  # cleared on leaving MANUAL_ACTION_REQUIRED

    app = transition(store, app, Status.SUBMITTED, "confirmed")
    history = store.history(app.id)
    assert [(h.from_status, h.to_status) for h in history] == [
        ("PENDING", "IN_PROGRESS"),
        ("IN_PROGRESS", "MANUAL_ACTION_REQUIRED"),
        ("MANUAL_ACTION_REQUIRED", "IN_PROGRESS"),
        ("IN_PROGRESS", "SUBMITTED"),
    ]
    assert history[1].reason == "otp at login"


def test_transition_rejects_illegal_move_without_writing(store):
    app = store.upsert_job("jk2")
    with pytest.raises(InvalidTransition):
        transition(store, app, Status.SUBMITTED, "skip ahead")
    assert store.get(app.id).status == Status.PENDING
    assert store.history(app.id) == []


def test_cannot_leave_terminal_state(store):
    app = store.upsert_job("jk3")
    app = transition(store, app, Status.IN_PROGRESS)
    app = transition(store, app, Status.FAILED, "selector missing")
    with pytest.raises(InvalidTransition):
        transition(store, app, Status.IN_PROGRESS, "retry")


def test_pending_straight_to_failed(store):
    app = store.upsert_job("jk4")
    app = transition(store, app, Status.FAILED, "posting 404 before work started")
    assert app.status == Status.FAILED
    assert store.history(app.id)[0].from_status == "PENDING"
