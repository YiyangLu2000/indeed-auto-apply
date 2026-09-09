"""Storage: schema, upsert, dedup, transition logging — all against a temp sqlite."""

from __future__ import annotations

import pytest

from indeed_apply.storage import Storage


@pytest.fixture()
def store(tmp_path):
    s = Storage(tmp_path / "apps.db")
    s.init_db()
    return s


def test_init_db_is_idempotent(tmp_path):
    s = Storage(tmp_path / "apps.db")
    s.init_db()
    s.init_db()  # no error on second call
    assert s.list() == []


def test_upsert_inserts_then_updates_metadata_not_status(store):
    a1 = store.upsert_job(
        "jk-abc", title="Backend Engineer", company="Acme", location="LA", url="u1"
    )
    assert a1.id == 1
    assert a1.status == "PENDING"
    assert a1.created_at == a1.updated_at

    # move it off PENDING via the raw recorder (state machine tested elsewhere)
    store.record_transition(a1.id, "PENDING", "IN_PROGRESS", "go")

    a2 = store.upsert_job("jk-abc", title="Senior Backend Engineer", company="Acme2")
    assert a2.id == a1.id  # same row
    assert a2.title == "Senior Backend Engineer"
    assert a2.company == "Acme2"
    assert a2.status == "IN_PROGRESS"  # upsert never resets status


def test_get_and_get_by_job_key(store):
    a = store.upsert_job("jk-1", title="X")
    assert store.get(a.id) == a
    assert store.get_by_job_key("jk-1") == a
    assert store.get(999) is None
    assert store.get_by_job_key("missing") is None


def test_dedup_helper(store):
    assert not store.has_job("jk-dup")
    store.upsert_job("jk-dup")
    assert store.has_job("jk-dup")


def test_list_filters_by_status(store):
    store.upsert_job("a")
    b = store.upsert_job("b")
    store.record_transition(b.id, "PENDING", "IN_PROGRESS", None)
    assert [x.job_key for x in store.list()] == ["a", "b"]
    assert [x.job_key for x in store.list("PENDING")] == ["a"]
    assert [x.job_key for x in store.list("IN_PROGRESS")] == ["b"]
    assert store.list("SUBMITTED") == []


def test_record_transition_updates_row_and_appends_history(store):
    a = store.upsert_job("jk-h")
    store.record_transition(a.id, "PENDING", "IN_PROGRESS", "start")
    store.record_transition(a.id, "IN_PROGRESS", "MANUAL_ACTION_REQUIRED", "captcha")

    row = store.get(a.id)
    assert row.status == "MANUAL_ACTION_REQUIRED"
    assert row.manual_reason == "captcha"
    assert row.updated_at >= row.created_at

    hist = store.history(a.id)
    assert len(hist) == 2
    assert hist[0].from_status == "PENDING"
    assert hist[0].to_status == "IN_PROGRESS"
    assert hist[1].reason == "captcha"


def test_manual_reason_cleared_when_leaving_manual_state(store):
    a = store.upsert_job("jk-m")
    store.record_transition(a.id, "PENDING", "IN_PROGRESS", None)
    store.record_transition(a.id, "IN_PROGRESS", "MANUAL_ACTION_REQUIRED", "otp")
    assert store.get(a.id).manual_reason == "otp"
    store.record_transition(a.id, "MANUAL_ACTION_REQUIRED", "IN_PROGRESS", "resumed")
    assert store.get(a.id).manual_reason is None


def test_record_transition_unknown_app_raises(store):
    with pytest.raises(KeyError):
        store.record_transition(424242, "PENDING", "IN_PROGRESS", None)


def test_job_key_unique(store):
    store.upsert_job("only-once")
    store.upsert_job("only-once")
    assert len(store.list()) == 1
