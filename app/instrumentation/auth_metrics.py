"""Authentication attempt outcome counter.

Import record_auth_attempt() at every authentication decision point and call it
with the outcome and (optionally) the denial reason.

Metric:
  auth.attempts  (Counter) — tagged with outcome (success|denied) and
                              reason (wrong_password|expired|invalid_signature|none)
"""
from opentelemetry import metrics

_meter = metrics.get_meter("app.instrumentation.auth")

auth_attempts = _meter.create_counter(
    name="auth.attempts",
    description="Total authentication/authorization decisions.",
    unit="{attempt}",
)


def record_auth_attempt(outcome: str, reason: str = "none") -> None:
    """Record one auth decision.

    Args:
        outcome: "success" or "denied"
        reason:  denial reason tag — e.g. "wrong_password", "expired",
                 "invalid_signature".  Use "none" for successful attempts.
    """
    auth_attempts.add(1, {"outcome": outcome, "reason": reason})
