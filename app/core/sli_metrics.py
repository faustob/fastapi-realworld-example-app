"""Application-level SLI instruments.

All instruments are defined here exactly once and imported at the
measurement sites.  The module uses the global MeterProvider so it
must be imported AFTER setup_telemetry() has been called.
"""
from __future__ import annotations

from opentelemetry import metrics

_meter = metrics.get_meter(__name__)

# ── HTTP saturation: in-flight requests (UpDownCounter — can go up and down) ──
http_active_requests = _meter.create_up_down_counter(
    name="http.server.active_requests",
    unit="{request}",
    description="Number of HTTP requests currently being processed.",
)

# ── Authentication attempt outcomes ──────────────────────────────────────────
auth_attempts = _meter.create_counter(
    name="auth.attempts",
    unit="{attempt}",
    description="Total authentication/authorisation decisions, tagged by outcome and reason.",
)

# ── E2E business-flow outcomes ────────────────────────────────────────────────
flow_outcomes = _meter.create_counter(
    name="flow.outcomes",
    unit="{flow}",
    description="Terminal outcomes of the registration-to-publish business flow.",
)

# ── E2E business-flow entry (throughput) ─────────────────────────────────────
flow_entries = _meter.create_counter(
    name="flow.entries",
    unit="{flow}",
    description="Number of times the primary business flow entry point was invoked.",
)

# ── E2E business-flow latency (entry-to-terminal) ────────────────────────────
flow_duration = _meter.create_histogram(
    name="flow.duration",
    unit="s",
    description="End-to-end duration of the primary business flow in seconds.",
)

# ── Flow entry-to-terminal freshness ─────────────────────────────────────────
flow_entry_to_terminal = _meter.create_histogram(
    name="flow.entry_to_terminal.duration",
    unit="s",
    description="Wall-clock time between flow entry and terminal state transition.",
)

# ── Flow validation outcomes ──────────────────────────────────────────────────
flow_validation_outcomes = _meter.create_counter(
    name="flow.validation.outcomes",
    unit="{check}",
    description="Per-step validation outcomes for the primary business flow.",
)
