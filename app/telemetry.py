"""Telemetry helpers — auth and flow instrumentation.

Import and call these helpers from the relevant route/service modules to
record auth attempt outcomes and primary-flow lifecycle events.
"""
import time
from opentelemetry import metrics, trace
from opentelemetry.trace import Status, StatusCode

_tracer = trace.get_tracer(__name__)

# Instruments are created lazily (on first use) so that get_meter() is called
# only after app.main has run metrics.set_meter_provider(), ensuring they bind
# to the real SDK MeterProvider rather than the default no-op proxy.

_auth_attempts = None
_flow_outcomes = None
_flow_entries = None
_flow_validation_outcomes = None
_flow_entry_to_terminal = None
_flow_duration = None


def _get_instruments():
    global _auth_attempts, _flow_outcomes, _flow_entries
    global _flow_validation_outcomes, _flow_entry_to_terminal, _flow_duration
    if _auth_attempts is None:
        _meter = metrics.get_meter(__name__)
        _auth_attempts = _meter.create_counter(
            "auth.attempts",
            unit="1",
            description="Authentication/authorization decisions tagged by outcome and reason",
        )
        _flow_outcomes = _meter.create_counter(
            "flow.outcomes",
            unit="1",
            description="Terminal outcomes for the registration-to-publish business flow",
        )
        _flow_entries = _meter.create_counter(
            "flow.entries",
            unit="1",
            description="Number of times the primary business flow entry point is invoked",
        )
        _flow_validation_outcomes = _meter.create_counter(
            "flow.validation.outcomes",
            unit="1",
            description="Per-step validation pass/fail outcomes for the primary flow",
        )
        _flow_entry_to_terminal = _meter.create_histogram(
            "flow.entry_to_terminal.duration",
            unit="s",
            description="Wall-clock time from flow entry to terminal state transition",
        )
        _flow_duration = _meter.create_histogram(
            "flow.duration",
            unit="s",
            description="End-to-end duration of the primary business flow",
        )


def record_auth_attempt(outcome: str, reason: str = "") -> None:
    """Record one authentication/authorization decision.

    Args:
        outcome: "success" or "denied"
        reason:  denial reason tag, e.g. "expired", "invalid_signature",
                 "wrong_password", or "" for successful attempts.
    """
    _get_instruments()
    _auth_attempts.add(1, {"outcome": outcome, "reason": reason})


def record_flow_entry(flow: str = "registration_to_publish") -> float:
    """Increment the flow-entry counter and return the entry timestamp.

    Call this at the very start of the primary business flow.
    Returns the monotonic clock value to pass to record_flow_terminal().
    """
    _get_instruments()
    _flow_entries.add(1, {"flow": flow})
    return time.perf_counter()


def record_flow_terminal(
    entry_ts: float,
    outcome: str,
    flow: str = "registration_to_publish",
    terminal_state: str = "completed",
) -> None:
    """Record the terminal outcome of the primary business flow.

    Args:
        entry_ts:       value returned by record_flow_entry().
        outcome:        "success" or "failure".
        flow:           flow identifier tag.
        terminal_state: e.g. "completed", "failed", "cancelled".
    """
    _get_instruments()
    elapsed = time.perf_counter() - entry_ts
    attrs = {"flow": flow, "outcome": outcome, "terminal_state": terminal_state}
    _flow_outcomes.add(1, attrs)
    _flow_duration.record(elapsed, attrs)
    _flow_entry_to_terminal.record(elapsed, {"flow": flow, "terminal_state": terminal_state})


def record_validation_step(
    step: str,
    passed: bool,
    flow: str = "registration_to_publish",
    flow_id: str = "",
) -> None:
    """Record the outcome of a single validation step within the primary flow.

    Also creates a child span with pass/fail attributes for trace-level triage.

    Args:
        step:    name of the validation step, e.g. "email_format", "username_unique".
        passed:  True if the step passed, False if it failed.
        flow:    flow identifier tag.
        flow_id: optional correlation ID shared across all steps of one flow instance.
    """
    _get_instruments()
    outcome = "passed" if passed else "failed"
    _flow_validation_outcomes.add(
        1,
        {"flow": flow, "step": step, "outcome": outcome, "flow_id": flow_id},
    )
    with _tracer.start_as_current_span(f"flow.validation.{step}") as span:
        span.set_attribute("flow", flow)
        span.set_attribute("flow_id", flow_id)
        span.set_attribute("validation.step", step)
        span.set_attribute("validation.outcome", outcome)
        if not passed:
            span.set_status(Status(StatusCode.ERROR, f"Validation step '{step}' failed"))
