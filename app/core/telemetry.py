"""OpenTelemetry SDK bootstrap and shared instrumentation helpers.

Call setup_telemetry() once at process startup (before any request is served).
All other modules obtain their tracer/meter via get_tracer(__name__) /
get_meter(__name__) — they never build providers themselves.
"""
import os
import time
from typing import Callable

from fastapi import FastAPI, Request, Response
from opentelemetry import trace, metrics
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource, SERVICE_NAME
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

# ---------------------------------------------------------------------------
# P99 budget in seconds — requests exceeding this get a span event
# ---------------------------------------------------------------------------
_P99_BUDGET_S = 0.750

_telemetry_initialised = False


def setup_telemetry() -> None:
    """Build and register the global TracerProvider and MeterProvider.

    Safe to call multiple times (idempotent after first call).
    The OTLP endpoint is read from OTEL_EXPORTER_OTLP_ENDPOINT (never
    hardcoded); if the env-var is absent the SDK still starts and exports
    to the default localhost:4317.
    """
    global _telemetry_initialised
    if _telemetry_initialised:
        return

    service_name = os.environ.get("OTEL_SERVICE_NAME", "fastapi-realworld-example-app")
    resource = Resource.create({SERVICE_NAME: service_name})

    # --- Traces ---
    otlp_span_exporter = OTLPSpanExporter()  # endpoint from env
    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(BatchSpanProcessor(otlp_span_exporter))
    trace.set_tracer_provider(tracer_provider)

    # --- Metrics ---
    otlp_metric_exporter = OTLPMetricExporter()  # endpoint from env
    metric_reader = PeriodicExportingMetricReader(otlp_metric_exporter)
    meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
    metrics.set_meter_provider(meter_provider)

    _telemetry_initialised = True


def instrument_app(app: FastAPI) -> None:
    """Attach all framework-level instrumentation to *app*.

    Must be called after setup_telemetry() and after all routes are
    registered so FastAPIInstrumentor can capture the route templates.
    """
    FastAPIInstrumentor.instrument_app(app)
    _add_saturation_middleware(app)
    _add_slow_request_middleware(app)


# ---------------------------------------------------------------------------
# Shared meter / instruments  (defined ONCE here, imported nowhere else)
# ---------------------------------------------------------------------------

def _get_meter():
    return metrics.get_meter(__name__)


# We build instruments lazily so they are created after setup_telemetry() has
# registered the real MeterProvider.
_meter = None
_active_requests_counter = None
_http_requests_counter = None
_auth_attempts_counter = None
_flow_outcomes_counter = None
_flow_entry_counter = None
_flow_duration_histogram = None
_flow_validation_outcomes_counter = None


def _ensure_instruments():
    global _meter
    global _active_requests_counter
    global _http_requests_counter
    global _auth_attempts_counter
    global _flow_outcomes_counter
    global _flow_entry_counter
    global _flow_duration_histogram
    global _flow_validation_outcomes_counter

    if _meter is not None:
        return

    _meter = metrics.get_meter(__name__)

    # SLI: HTTP Worker Pool Saturation — in-flight requests (UpDownCounter)
    _active_requests_counter = _meter.create_up_down_counter(
        name="http.server.active_requests",
        description="Number of HTTP requests currently being processed",
        unit="{request}",
    )

    # SLI: HTTP Request Throughput / Availability — request outcome counter
    _http_requests_counter = _meter.create_counter(
        name="http.server.requests.total",
        description="Total HTTP requests, labelled by route, method and outcome",
        unit="{request}",
    )

    # SLI: Authentication Failure Rate
    _auth_attempts_counter = _meter.create_counter(
        name="auth.attempts.total",
        description="Authentication/authorisation decisions, labelled by outcome and reason",
        unit="{attempt}",
    )

    # SLI: E2E Business Flow Success Rate / Throughput
    _flow_outcomes_counter = _meter.create_counter(
        name="flow.outcomes.total",
        description="Terminal outcomes for the registration-to-publish flow",
        unit="{flow}",
    )

    # SLI: E2E Business Flow Throughput (entry)
    _flow_entry_counter = _meter.create_counter(
        name="flow.entries.total",
        description="Number of times the primary flow entry point was invoked",
        unit="{flow}",
    )

    # SLI: Flow Completion Freshness / E2E Latency P95
    _flow_duration_histogram = _meter.create_histogram(
        name="flow.entry_to_terminal.duration",
        description="Wall-clock seconds from flow entry to terminal state",
        unit="s",
    )

    # SLI: Flow Validation Failure Rate
    _flow_validation_outcomes_counter = _meter.create_counter(
        name="flow.validation.outcomes.total",
        description="Per-step validation outcomes for the primary flow",
        unit="{check}",
    )


# ---------------------------------------------------------------------------
# Public helpers called by route/auth code
# ---------------------------------------------------------------------------

def record_auth_attempt(outcome: str, reason: str = "") -> None:
    """Record one auth attempt.

    outcome: "success" | "denied"
    reason:  "" | "expired" | "invalid_signature" | "wrong_password" | ...
    """
    _ensure_instruments()
    attrs = {"auth.outcome": outcome}
    if reason:
        attrs["auth.denial_reason"] = reason
    _auth_attempts_counter.add(1, attrs)


def record_flow_entry(flow: str = "registration_to_publish") -> float:
    """Record that the primary flow was entered.  Returns entry timestamp."""
    _ensure_instruments()
    _flow_entry_counter.add(1, {"flow": flow})
    return time.monotonic()


def record_flow_outcome(
    outcome: str,
    flow: str = "registration_to_publish",
    entry_time: float = 0.0,
    terminal_state: str = "",
) -> None:
    """Record the terminal outcome of the primary flow.

    outcome: "success" | "failure"
    entry_time: value returned by record_flow_entry() (0 to skip duration)
    terminal_state: e.g. "article_published" | "error"
    """
    _ensure_instruments()
    _flow_outcomes_counter.add(1, {"flow": flow, "outcome": outcome})
    if entry_time:
        elapsed = time.monotonic() - entry_time
        attrs = {"flow": flow}
        if terminal_state:
            attrs["terminal_state"] = terminal_state
        _flow_duration_histogram.record(elapsed, attrs)


def record_flow_validation(step: str, passed: bool, flow_id: str = "") -> None:
    """Record the outcome of one validation step inside the primary flow."""
    _ensure_instruments()
    attrs = {
        "flow.step": step,
        "validation.outcome": "passed" if passed else "failed",
    }
    if flow_id:
        attrs["flow.id"] = flow_id
    _flow_validation_outcomes_counter.add(1, attrs)


# ---------------------------------------------------------------------------
# Middleware helpers (wired by instrument_app)
# ---------------------------------------------------------------------------

def _add_saturation_middleware(app: FastAPI) -> None:
    """Middleware that tracks in-flight requests via an UpDownCounter."""

    @app.middleware("http")
    async def saturation_middleware(request: Request, call_next: Callable) -> Response:
        _ensure_instruments()
        route = _route_template(request)
        attrs = {"http.route": route, "http.request.method": request.method}
        _active_requests_counter.add(1, attrs)
        try:
            response = await call_next(request)
        finally:
            _active_requests_counter.add(-1, attrs)
        return response


def _add_slow_request_middleware(app: FastAPI) -> None:
    """Middleware that adds a span event for requests exceeding the P99 budget
    and records the per-route request outcome counter."""

    @app.middleware("http")
    async def slow_request_middleware(request: Request, call_next: Callable) -> Response:
        _ensure_instruments()
        start = time.monotonic()
        response = await call_next(request)
        elapsed = time.monotonic() - start

        route = _route_template(request)
        status_code = response.status_code
        outcome = "5xx" if status_code >= 500 else ("4xx" if status_code >= 400 else "2xx")

        # Request outcome counter (availability + throughput SLIs)
        _http_requests_counter.add(
            1,
            {
                "http.route": route,
                "http.request.method": request.method,
                "http.response.status_code": str(status_code),
                "http.outcome": outcome,
            },
        )

        # Slow-request span event (P99 SLI)
        if elapsed > _P99_BUDGET_S:
            span = trace.get_current_span()
            span.add_event(
                "slow_request",
                {
                    "http.route": route,
                    "http.request.method": request.method,
                    "http.response.status_code": status_code,
                    "duration_s": elapsed,
                    "p99_budget_s": _P99_BUDGET_S,
                },
            )

        return response


def _route_template(request: Request) -> str:
    """Return the matched route template, falling back to a safe placeholder.

    Using the template (e.g. /api/articles/{slug}) rather than the raw path
    keeps metric cardinality bounded.
    """
    route = request.scope.get("route")
    if route and hasattr(route, "path"):
        return route.path
    # Before routing resolves (e.g. 404) use a safe placeholder
    return "unknown"
