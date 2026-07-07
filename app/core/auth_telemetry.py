"""Helpers for recording authentication attempt outcomes.

Import record_auth_attempt() from any authentication code path and call it
after the decision is made.  The instruments are owned by telemetry.py;
this module only provides a thin recording helper so auth code does not
need to import the full telemetry module.
"""
from opentelemetry import trace


def record_auth_attempt(outcome: str, reason: str = "") -> None:
    """Record one authentication decision.

    Parameters
    ----------
    outcome:
        ``"success"`` or ``"denied"``.
    reason:
        Low-cardinality denial reason, e.g. ``"expired"``,
        ``"invalid_signature"``, ``"wrong_password"``.  Empty string for
        successful attempts.
    """
    # Import lazily to avoid a circular import at module load time.
    from app.core.telemetry import _auth_attempts_counter  # noqa: PLC0415

    if _auth_attempts_counter is None:
        # SDK not yet initialised (e.g. during unit tests without OTel).
        return

    attrs: dict = {"outcome": outcome}
    if reason:
        attrs["denial.reason"] = reason
    _auth_attempts_counter.add(1, attrs)

    # Also annotate the current span so 5xx / auth errors are attributable.
    if outcome == "denied":
        span = trace.get_current_span()
        span.set_attribute("error.type", reason or "auth_denied")
