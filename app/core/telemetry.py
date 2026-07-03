"""OpenTelemetry SDK bootstrap and shared instrumentation helpers.

Call setup_telemetry() once at process startup (before any request is served).
All other modules obtain their tracer/meter via get_tracer/get_meter so the
providers are never initialised more than once.
"""
import os
import time
from typing import Callable

from fastapi import FastAPI, Request, Response
from opentelemetry import metrics, trace
from opentelemetry.trace import Status, StatusCode
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource, SERVICE_NAME
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_SERVICE_NAME = os.environ.get("OTEL_SERVICE_NAME", "fastapi-realworld-example-app")
_OTLP_ENDPOINT = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")

# P99 budget in seconds — requests exceeding this get a span event
_P99_BUDGET_S = float(os.environ.get("P99_BUDGET_S", "0.75"))

_setup_done = False


def setup_telemetry() -> None:
    """Initialise and globally register TracerProvider + MeterProvider.

    Safe to call multiple times; subsequent calls are no-ops.
    """
    global _setup_done
    if _setup_done:
        return

    resource = Resource.create({SERVICE_NAME: _SERVICE_NAME})

    # --- Traces ---
    tracer_provider = TracerProvider(resource=resource)
    span_exporter_kwargs = {"endpoint": _OTLP_ENDPOINT} if _OTLP_ENDPOINT else {}
    tracer_provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(**span_exporter_kwargs))
    )
    trace.set_tracer_provider(tracer_provider)

    # --- Metrics ---
    metric_exporter_kwargs = {"endpoint": _OTLP_ENDPOINT} if _OTLP_ENDPOINT else {}
    metric_reader = PeriodicExportingMetricReader(
        OTLPMetricExporter(**metric_exporter_kwargs)
    )
    meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
    metrics.set_meter_provider(meter_provider)

    _setup_done = True


def instrument_app(app: FastAPI) -> None:
    """Attach FastAPIInstrumentor and the active-requests middleware."""
    FastAPIInstrumentor.instrument_app(app)
    app.middleware("http")(_active_requests_middleware)


# ---------------------------------------------------------------------------
# Shared meter / instruments  (created lazily after setup_telemetry() runs)
# ---------------------------------------------------------------------------

def _get_meter():
    return metrics.get_meter(__name__)


# We use module-level singletons so instruments are created exactly once.
_meter = None
_active_requests_counter = None
_http_requests_counter = None
_auth_attempts_counter = None
_flow_outcomes_counter = None
_flow_entries_counter = None
_flow_validation_outcomes_counter = None
_flow_duration_histogram = None
_flow_entry_to_terminal_histogram = None


def _ensure_instruments() -> None:
    global _meter
    global _active_requests_counter
    global _http_requests_counter
    global _auth_attempts_counter
    global _flow_outcomes_counter
    global _flow_entries_counter
    global _flow_validation_outcomes_counter
    global _flow_duration_histogram
    global _flow_entry_to_terminal_histogram

    if _meter is not None:
        return

    _meter = metrics.get_meter(__name__)

    # HTTP saturation — in-flight requests (UpDownCounter because it goes up AND down)
    _active_requests_counter = _meter.create_up_down_counter(
        name="http.server.active_requests",
        description="Number of HTTP requests currently being processed",
        unit="{request}",
    )

    # HTTP request throughput / availability outcome counter
    _http_requests_counter = _meter.create_counter(
        name="http.server.requests.total",
        description="Total HTTP requests, labelled by route, method and outcome class",
        unit="{request}",
    )

    # Auth attempt outcome counter
    _auth_attempts_counter = _meter.create_counter(
        name="auth.attempts.total",
        description="Authentication/authorisation decisions, labelled by outcome and reason",
        unit="{attempt}",
    )

    # E2E flow outcome counter
    _flow_outcomes_counter = _meter.create_counter(
        name="flow.outcomes.total",
        description="Terminal outcomes for the primary registration-to-publish flow",
        unit="{flow}",
    )

    # E2E flow entry counter (throughput)
    _flow_entries_counter = _meter.create_counter(
        name="flow.entries.total",
        description="Number of times the primary flow entry point was invoked",
        unit="{flow}",
    )

    # Flow validation outcome counter
    _flow_validation_outcomes_counter = _meter.create_counter(
        name="flow.validation.outcomes.total",
        description="Per-step validation outcomes for the primary flow",
        unit="{check}",
    )

    # E2E flow duration histogram (latency P95)
    _flow_duration_histogram = _meter.create_histogram(
        name="flow.duration",
        description="End-to-end duration of the primary flow",
        unit="s",
    )

    # Entry-to-terminal duration histogram (freshness)
    _flow_entry_to_terminal_histogram = _meter.create_histogram(
        name="flow.entry_to_terminal.duration",
        description="Wall-clock time from flow entry to terminal state transition",
        unit="s",
    )


# ---------------------------------------------------------------------------
# Middleware — active requests + per-request metrics
# ---------------------------------------------------------------------------

async def _active_requests_middleware(request: Request, call_next: Callable) -> Response:
    """ASGI middleware that tracks in-flight requests and records per-request
    outcome counters for availability and throughput SLIs.
    """
    _ensure_instruments()

    route = _get_route_template(request)
    method = request.method
    common_attrs = {"http.request.method": method, "http.route": route}

    _active_requests_counter.add(1, common_attrs)
    start = time.perf_counter()
    try:
        response: Response = await call_next(request)
    except Exception:
        elapsed = time.perf_counter() - start
        _active_requests_counter.add(-1, common_attrs)
        raise
    else:
        elapsed = time.perf_counter() - start
        _active_requests_counter.add(-1, common_attrs)

    status_code = response.status_code
    outcome = "5xx" if status_code >= 500 else ("4xx" if status_code >= 400 else "2xx")
    _http_requests_counter.add(
        1,
        {
            "http.request.method": method,
            "http.route": route,
            "http.response.status_code": status_code,
            "outcome": outcome,
        },
    )

    # Slow-request span event for P99 triage
    if elapsed > _P99_BUDGET_S:
        current_span = trace.get_current_span()
        if current_span.is_recording():
            current_span.add_event(
                "slow_request",
                {
                    "http.route": route,
                    "http.request.method": method,
                    "duration_s": elapsed,
                    "p99_budget_s": _P99_BUDGET_S,
                },
            )

    # Exception-to-status mapping: set error attributes on the active span for 5xx
    if status_code >= 500:
        current_span = trace.get_current_span()
        if current_span.is_recording():
            current_span.set_status(Status(StatusCode.ERROR, f"HTTP {status_code}"))

    return response


def _get_route_template(request: Request) -> str:
    """Return the matched route template, falling back to a safe placeholder."""
    # FastAPI populates request.scope["route"] after routing
    route = request.scope.get("route")
    if route is not None and hasattr(route, "path"):
        return route.path
    return "unknown"


# ---------------------------------------------------------------------------
# Public helpers for use by other modules
# ---------------------------------------------------------------------------

def record_auth_attempt(outcome: str, reason: str) -> None:
    """Record one authentication/authorisation decision.

    Args:
        outcome: "success" or "denied"
        reason:  e.g. "expired", "invalid_signature", "wrong_password", "ok"
    """
    _ensure_instruments()
    _auth_attempts_counter.add(1, {"outcome": outcome, "reason": reason})


def record_flow_entry(flow: str = "registration_to_publish") -> float:
    """Increment the flow-entry counter and return the current timestamp.

    Returns the entry timestamp (time.perf_counter()) so the caller can later
    pass it to record_flow_outcome().
    """
    _ensure_instruments()
    _flow_entries_counter.add(1, {"flow": flow})
    return time.perf_counter()


def record_flow_outcome(
    outcome: str,
    entry_timestamp: float,
    flow: str = "registration_to_publish",
    terminal_state: str = "completed",
) -> None:
    """Record the terminal outcome of a flow and its duration.

    Args:
        outcome:         "success" or "failure"
        entry_timestamp: value returned by record_flow_entry()
        flow:            flow identifier
        terminal_state:  e.g. "completed", "failed", "cancelled"
    """
    _ensure_instruments()
    elapsed = time.perf_counter() - entry_timestamp
    attrs = {"flow": flow, "outcome": outcome, "terminal_state": terminal_state}
    _flow_outcomes_counter.add(1, attrs)
    _flow_duration_histogram.record(elapsed, attrs)
    _flow_entry_to_terminal_histogram.record(elapsed, {"flow": flow, "terminal_state": terminal_state})


def record_flow_validation(
    step: str,
    outcome: str,
    flow_id: str = "",
    flow: str = "registration_to_publish",
) -> None:
    """Record the outcome of a single validation step within a flow.

    Also sets a pass/fail attribute on the current span.

    Args:
        step:     name of the validation step (e.g. "email_unique", "password_strength")
        outcome:  "passed" or "failed"
        flow_id:  optional correlation ID for the flow instance
        flow:     flow identifier
    """
    _ensure_instruments()
    _flow_validation_outcomes_counter.add(
        1,
        {"flow": flow, "step": step, "outcome": outcome},
    )
    current_span = trace.get_current_span()
    if current_span.is_recording():
        current_span.set_attribute("flow.validation.step", step)
        current_span.set_attribute("flow.validation.outcome", outcome)
        if flow_id:
            current_span.set_attribute("flow.id", flow_id)
