"""Business-flow instrumentation for the primary registration-to-publish flow.

Metrics emitted:
  flow.outcomes                  (Counter)   — terminal outcome per flow
  flow.duration                  (Histogram) — end-to-end flow duration in seconds
  flow.entry_to_terminal.duration (Histogram) — wall-clock entry→terminal in seconds
  flow.validation.outcomes       (Counter)   — per-step validation pass/fail
  flow.entries                   (Counter)   — flow entry invocations (throughput)
"""
import time
from opentelemetry import metrics

_meter = metrics.get_meter("app.instrumentation.flow")

flow_outcomes = _meter.create_counter(
    name="flow.outcomes",
    description="Terminal outcome of the primary registration-to-publish flow.",
    unit="{flow}",
)

flow_duration = _meter.create_histogram(
    name="flow.duration",
    description="End-to-end duration of the primary flow.",
    unit="s",
)

flow_entry_to_terminal_duration = _meter.create_histogram(
    name="flow.entry_to_terminal.duration",
    description="Wall-clock time from flow entry to terminal state transition.",
    unit="s",
)

flow_validation_outcomes = _meter.create_counter(
    name="flow.validation.outcomes",
    description="Per-step validation pass/fail outcomes within the primary flow.",
    unit="{check}",
)

flow_entries = _meter.create_counter(
    name="flow.entries",
    description="Number of times the primary flow entry point has been invoked.",
    unit="{flow}",
)


def record_flow_entry(flow: str = "primary") -> float:
    """Call at the flow entry point.  Returns the entry timestamp for later use."""
    flow_entries.add(1, {"flow": flow})
    return time.time()


def record_flow_outcome(
    outcome: str,
    flow: str = "primary",
    entry_timestamp: float = 0.0,
) -> None:
    """Call at the terminal state of the flow.

    Args:
        outcome:         "success" or "failure"
        flow:            flow identifier tag
        entry_timestamp: value returned by record_flow_entry(); 0 skips duration recording
    """
    flow_outcomes.add(1, {"outcome": outcome, "flow": flow})
    if entry_timestamp:
        elapsed = time.time() - entry_timestamp
        flow_duration.record(elapsed, {"flow": flow, "outcome": outcome})
        flow_entry_to_terminal_duration.record(elapsed, {"flow": flow, "terminal_state": outcome})


def record_validation_outcome(step: str, outcome: str, flow: str = "primary") -> None:
    """Record a single validation step result.

    Args:
        step:    name of the validation step (e.g. "email_format", "username_unique")
        outcome: "passed" or "failed"
        flow:    flow identifier tag
    """
    flow_validation_outcomes.add(1, {"step": step, "outcome": outcome, "flow": flow})
