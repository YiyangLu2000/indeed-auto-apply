"""Typed errors shared across the module.

Kept in one place so the CLI can catch them precisely and print clean,
actionable messages instead of tracebacks.
"""

from __future__ import annotations


class IndeedApplyError(Exception):
    """Base class for every error this module raises deliberately."""


class InvalidTransition(IndeedApplyError):
    """A status change was requested that the state machine does not allow."""

    def __init__(self, from_state: str, to_state: str) -> None:
        self.from_state = from_state
        self.to_state = to_state
        super().__init__(
            f"illegal transition {from_state!r} -> {to_state!r}"
        )


class ManualActionRequired(IndeedApplyError):
    """A human must act before automation can continue.

    Raised when Indeed shows a verification / anti-bot / login wall, or when a
    screening question cannot be answered confidently. ``reason`` is a short,
    human-readable string suitable for storing in ``applications.manual_reason``
    and printing to the operator.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class SessionExpired(IndeedApplyError):
    """The restored Indeed session is no longer logged in.

    Distinct from :class:`ManualActionRequired`: the fix is simply to re-run
    ``login`` + ``capture-session``, not to solve a challenge.
    """

    def __init__(self, reason: str = "session expired") -> None:
        self.reason = reason
        super().__init__(reason)


class SessionMissing(IndeedApplyError):
    """No captured session blob is present on disk."""


class ProfileError(IndeedApplyError):
    """The candidate profile is missing, unparseable, or incomplete."""


class ConfigError(IndeedApplyError):
    """Required configuration (e.g. an encryption key) could not be resolved."""
