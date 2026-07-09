"""Shared OTel instruments for business/domain-level SLIs.

These instruments are defined once here and recorded from the route
handlers / auth logic that own the corresponding events.
"""
from app.core.telemetry import get_meter

_meter = get_meter()

# Authentication Failure Rate SLI
auth_attempts_total = _meter.create_counter(
    name="auth.attempts.total",
    unit="1",
    description="Count of authentication attempts, tagged by outcome and denial reason",
)

# Primary flow: registration -> publish
flow_outcomes_total = _meter.create_counter(
    name="flow.outcomes.total",
    unit="1",
    description="Terminal outcome count for the primary registration-to-publish flow",
)

flow_entries_total = _meter.create_counter(
    name="flow.entries.total",
    unit="1",
    description="Count of entries into the primary registration-to-publish flow",
)

flow_duration = _meter.create_histogram(
    name="flow.duration",
    unit="s",
    description="End-to-end duration of the primary registration-to-publish flow",
)

flow_validation_outcomes_total = _meter.create_counter(
    name="flow.validation.outcomes.total",
    unit="1",
    description="Per-step validation outcome count within the primary flow",
)

flow_entry_to_terminal_duration = _meter.create_histogram(
    name="flow.entry_to_terminal.duration",
    unit="s",
    description="Wall-clock time between flow entry event and terminal state transition",
)

# HTTP request outcome / throughput dimensions (supplements auto http.server.request.duration)
http_requests_by_tenant_total = _meter.create_counter(
    name="http.requests.by_tenant.total",
    unit="1",
    description="HTTP request count broken out by tenant/API key for per-tenant throughput SLIs",
)

# Worker pool saturation
http_server_active_requests = _meter.create_up_down_counter(
    name="http.server.active_requests",
    unit="1",
    description="Number of in-flight HTTP requests currently being served",
)
