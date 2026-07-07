"""Helpers for recording primary-flow (registration-to-publish) telemetry.

Each helper is a thin wrapper around the instruments defined in
app.core.telemetry so that route handlers stay readable.
"""
import time
from typing import Optional

from opentelemetry import trace


def record_flow_outcome(outcome: str, flow: str = "registration_to_publish") -> None:
    """Increment the flow.outcomes counter.

    Parameters
    ----------
    outcome:
        ``"success"`` or ``"failure"``.
    flow:
        Low-cardinality flow identifier.
    """
    from app.core.telemetry import flow_outcomes_counter  # noqa: PLC0415

    if flow_outcomes_counter is None:
        return
    flow_outcomes_counter.add(1, {"outcome": outcome, "flow": flow})


def record_flow_duration(elapsed_seconds: float, flow: str = "registration_to_publish", outcome: str = "success") -> None:
    """Record end-to-end flow duration in the flow.duration histogram."""
    from app.core.telemetry import flow_duration_histogram  # noqa: PLC0415

    if flow_duration_histogram is None:
        return
    flow_duration_histogram.record(elapsed_seconds, {"flow": flow, "outcome": outcome})


def record_entry_to_terminal(elapsed_seconds: float, flow: str = "registration_to_publish", terminal_state: str = "published") -> None:
    """Record entry-to-terminal wall-clock time in the flow.entry_to_terminal.duration histogram."""
    from app.core.telemetry import flow_entry_to_terminal_histogram  # noqa: PLC0415

    if flow_entry_to_terminal_histogram is None:
        return
    flow_entry_to_terminal_histogram.record(
        elapsed_seconds, {"flow": flow, "terminal_state": terminal_state}
    )


def record_validation_outcome(step: str, outcome: str, flow: str = "registration_to_publish") -> None:
    """Record a per-step validation outcome in the flow.validation.outcomes counter."""
    from app.core.telemetry import flow_validation_outcomes_counter  # noqa: PLC0415

    if flow_validation_outcomes_counter is None:
        return
    flow_validation_outcomes_counter.add(
        1, {"step": step, "outcome": outcome, "flow": flow}
    )
