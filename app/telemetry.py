"""OpenTelemetry instrumentation helpers.

Import this module ONCE at application startup (it is imported by app/main.py).
All instruments are module-level singletons so they are created only once.
"""
from __future__ import annotations

import time
from typing import Callable

from opentelemetry import metrics, trace

# ---------------------------------------------------------------------------
# Tracer / Meter
# ---------------------------------------------------------------------------
tracer = trace.get_tracer(__name__)


def _meter() -> metrics.Meter:
    """Return the global meter; called lazily so the SDK is already registered."""
    return metrics.get_meter(__name__)


# ---------------------------------------------------------------------------
# Lazy instrument accessors — instruments are created on first call, after
# configure_telemetry() has registered the real MeterProvider globally.
# ---------------------------------------------------------------------------
_http_requests_total = None
_http_active_requests = None
_auth_attempts_total = None
_flow_outcomes_total = None
_flow_entries_total = None
_flow_validation_outcomes_total = None
_flow_duration_histogram = None
_flow_entry_to_terminal_histogram = None


def _get_http_requests_total():
    global _http_requests_total
    if _http_requests_total is None:
        _http_requests_total = _meter().create_counter(
            "http.server.requests.total",
            unit="{request}",
            description="Total HTTP requests, labelled by route, method and outcome class.",
        )
    return _http_requests_total


def _get_http_active_requests():
    global _http_active_requests
    if _http_active_requests is None:
        _http_active_requests = _meter().create_up_down_counter(
            "http.server.active_requests",
            unit="{request}",
            description="Number of HTTP requests currently being processed.",
        )
    return _http_active_requests


def _get_auth_attempts_total():
    global _auth_attempts_total
    if _auth_attempts_total is None:
        _auth_attempts_total = _meter().create_counter(
            "auth.attempts.total",
            unit="{attempt}",
            description="Total authentication attempts, labelled by outcome and reason.",
        )
    return _auth_attempts_total


def _get_flow_outcomes_total():
    global _flow_outcomes_total
    if _flow_outcomes_total is None:
        _flow_outcomes_total = _meter().create_counter(
            "flow.outcomes.total",
            unit="{flow}",
            description="Terminal outcome counter for the registration-to-publish flow.",
        )
    return _flow_outcomes_total


def _get_flow_entries_total():
    global _flow_entries_total
    if _flow_entries_total is None:
        _flow_entries_total = _meter().create_counter(
            "flow.entries.total",
            unit="{flow}",
            description="Incremented every time the primary flow entry point is invoked.",
        )
    return _flow_entries_total


def _get_flow_validation_outcomes_total():
    global _flow_validation_outcomes_total
    if _flow_validation_outcomes_total is None:
        _flow_validation_outcomes_total = _meter().create_counter(
            "flow.validation.outcomes.total",
            unit="{check}",
            description="Per-step validation outcome counter for the primary flow.",
        )
    return _flow_validation_outcomes_total


def _get_flow_duration_histogram():
    global _flow_duration_histogram
    if _flow_duration_histogram is None:
        _flow_duration_histogram = _meter().create_histogram(
            "flow.duration",
            unit="s",
            description="End-to-end duration of the primary flow from entry to terminal state.",
        )
    return _flow_duration_histogram


def _get_flow_entry_to_terminal_histogram():
    global _flow_entry_to_terminal_histogram
    if _flow_entry_to_terminal_histogram is None:
        _flow_entry_to_terminal_histogram = _meter().create_histogram(
            "flow.entry_to_terminal.duration",
            unit="s",
            description="Wall-clock time between flow entry event and terminal state transition.",
        )
    return _flow_entry_to_terminal_histogram


# ---------------------------------------------------------------------------
# Public module-level names kept for backward compatibility with importers
# ---------------------------------------------------------------------------
class _LazyCounter:
    def __init__(self, getter): self._getter = getter
    def add(self, amount, attributes=None): self._getter().add(amount, attributes)

class _LazyHistogram:
    def __init__(self, getter): self._getter = getter
    def record(self, amount, attributes=None): self._getter().record(amount, attributes)


http_requests_total = _LazyCounter(_get_http_requests_total)
http_active_requests = _LazyCounter(_get_http_active_requests)
auth_attempts_total = _LazyCounter(_get_auth_attempts_total)
flow_outcomes_total = _LazyCounter(_get_flow_outcomes_total)
flow_entries_total = _LazyCounter(_get_flow_entries_total)
flow_validation_outcomes_total = _LazyCounter(_get_flow_validation_outcomes_total)
flow_duration_histogram = _LazyHistogram(_get_flow_duration_histogram)
flow_entry_to_terminal_histogram = _LazyHistogram(_get_flow_entry_to_terminal_histogram)

# ---------------------------------------------------------------------------
# P99 slow-request budget (seconds)
# ---------------------------------------------------------------------------
P99_BUDGET_S: float = 0.750


def record_http_request(
    method: str,
    route: str,
    status_code: int,
    duration_s: float,
) -> None:
    """Record per-request HTTP SLI metrics."""
    outcome = "5xx" if status_code >= 500 else ("4xx" if status_code >= 400 else "2xx")
    attrs = {
        "http.request.method": method,
        "http.route": route,
        "http.response.status_code": status_code,
        "http.outcome": outcome,
    }
    http_requests_total.add(1, attrs)

    # Slow-request span event for P99 triage
    if duration_s > P99_BUDGET_S:
        span = trace.get_current_span()
        span.add_event(
            "slow_request",
            {
                "http.route": route,
                "http.request.method": method,
                "http.response.status_code": status_code,
                "duration_s": duration_s,
                "p99_budget_s": P99_BUDGET_S,
            },
        )
