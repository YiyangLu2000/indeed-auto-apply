"""The single authority on application status and legal transitions.

No module writes ``applications.status`` directly. They call
:func:`transition`, which validates the change against :data:`ALLOWED`, raises
:class:`~indeed_apply.errors.InvalidTransition` on anything illegal, and
otherwise persists it through :meth:`Storage.record_transition` (row update +
``status_history`` append, atomic).
"""

from __future__ import annotations

from enum import StrEnum

from .errors import InvalidTransition
from .storage import Application, Storage


class Status(StrEnum):
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    MANUAL_ACTION_REQUIRED = "MANUAL_ACTION_REQUIRED"
    SUBMITTED = "SUBMITTED"
    FAILED = "FAILED"


#: Terminal states — nothing transitions out of these.
TERMINAL: frozenset[Status] = frozenset({Status.SUBMITTED, Status.FAILED})

#: Every legal (from, to) pair. Mirrors the table in CLAUDE.md §5.5.
ALLOWED: frozenset[tuple[Status, Status]] = frozenset(
    {
        (Status.PENDING, Status.IN_PROGRESS),
        (Status.PENDING, Status.FAILED),
        (Status.IN_PROGRESS, Status.MANUAL_ACTION_REQUIRED),
        (Status.IN_PROGRESS, Status.SUBMITTED),
        (Status.IN_PROGRESS, Status.FAILED),
        (Status.MANUAL_ACTION_REQUIRED, Status.IN_PROGRESS),
        (Status.MANUAL_ACTION_REQUIRED, Status.FAILED),
    }
)


def can_transition(from_state: str, to_state: str) -> bool:
    """True iff ``from_state -> to_state`` is a legal transition."""
    try:
        pair = (Status(from_state), Status(to_state))
    except ValueError:
        return False
    return pair in ALLOWED


def is_terminal(state: str) -> bool:
    try:
        return Status(state) in TERMINAL
    except ValueError:
        return False


def transition(
    storage: Storage,
    app: Application,
    to_state: str | Status,
    reason: str | None = None,
) -> Application:
    """Validate and persist ``app``'s move to ``to_state``.

    Returns the refreshed :class:`Application` row. Raises
    :class:`InvalidTransition` (and writes nothing) if the move is not in
    :data:`ALLOWED` — including any attempt to leave a terminal state or to use
    an unknown status string.
    """
    to_status = Status(str(to_state))
    from_status = app.status

    if not can_transition(from_status, to_status):
        raise InvalidTransition(from_status, to_status)

    storage.record_transition(app.id, from_status, to_status.value, reason)
    refreshed = storage.get(app.id)
    assert refreshed is not None  # row existed a line ago
    return refreshed
