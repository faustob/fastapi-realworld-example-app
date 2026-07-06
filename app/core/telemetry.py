"""OpenTelemetry SDK bootstrap and shared instrument definitions.

Call setup_telemetry(app) exactly once at application startup (from
get_application() in app/main.py).  All other modules obtain meters/tracers
via get_tracer() / get_meter() which delegate to the already-registered
global providers.
"""
import os
import time
from typing import Callable

from fastapi import FastAPI, Request, Response
from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.routing import Match

# ---------------------------------------------------------------------------
# Internal state – set once by setup_telemetry()
# ---------------------------------------------------------------------------
_meter: metrics.Meter | None = None
_tracer: trace.Tracer | None = None

# ---------------------------------------------------------------------------
# Public accessors (used by other modules)
# ---------------------------------------------------------------------------

def get_tracer() -> trace.Tracer:
    """Return the global tracer (no-op until setup_telemetry has been called)."""
    return trace.get_tracer(__name__)


def get_meter() -> metrics.Meter:
    """Return the global meter (no-op until setup_telemetry has been called)."""
    return metrics.get_meter(__name__)


# ---------------------------------------------------------------------------
# Instruments – defined once here, recorded in the middleware below
# ---------------------------------------------------------------------------

# Populated after setup_telemetry() initialises the MeterProvider.
_active_requests_counter = None   # UpDownCounter
_auth_attempts_counter = None     # Counter
_flow_outcomes_counter = None     # Counter
_flow_duration_histogram = None   # Histogram
_flow_entry_counter = None        # Counter
_flow_validation_counter = None   # Counter


# ---------------------------------------------------------------------------
# Saturation middleware
# ---------------------------------------------------------------------------

class _ActiveRequestsMiddleware(BaseHTTPMiddleware):
    """Tracks in-flight requests via an UpDownCounter."""

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        if _active_requests_counter is not None:
            _active_requests_counter.add(1, {"http.request.method": request.method})
        try:
            response = await call_next(request)
        finally:
            if _active_requests_counter is not None:
                _active_requests_counter.add(-1, {"http.request.method": request.method})
        return response


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

def setup_telemetry(app: FastAPI) -> None:
    """Initialise the OTel SDK, register global providers, and instrument *app*.

    Safe to call multiple times (subsequent calls are no-ops).
    """
    global _meter, _tracer
    global _active_requests_counter, _auth_attempts_counter
    global _flow_outcomes_counter, _flow_duration_histogram
    global _flow_entry_counter, _flow_validation_counter

    if _meter is not None:
        # Already initialised.
        return

    service_name = os.environ.get("OTEL_SERVICE_NAME", "fastapi-realworld-example-app")
    otlp_endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")

    resource = Resource.create({"service.name": service_name})

    # --- Traces ---
    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=otlp_endpoint))
    )
    trace.set_tracer_provider(tracer_provider)

    # --- Metrics ---
    metric_reader = PeriodicExportingMetricReader(
        OTLPMetricExporter(endpoint=otlp_endpoint)
    )
    meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
    metrics.set_meter_provider(meter_provider)

    _tracer = trace.get_tracer(__name__)
    _meter = metrics.get_meter(__name__)

    # --- Instruments ---

    # Saturation: in-flight requests (UpDownCounter — value can decrease)
    _active_requests_counter = _meter.create_up_down_counter(
        name="http.server.active_requests",
        unit="{request}",
        description="Number of HTTP requests currently being processed.",
    )

    # Auth attempt outcomes
    _auth_attempts_counter = _meter.create_counter(
        name="auth.attempts",
        unit="{attempt}",
        description="Count of authentication/authorisation decisions.",
    )

    # E2E flow outcomes
    _flow_outcomes_counter = _meter.create_counter(
        name="flow.outcomes",
        unit="{flow}",
        description="Terminal outcomes of the registration-to-publish business flow.",
    )

    # E2E flow duration (entry-to-terminal)
    _flow_duration_histogram = _meter.create_histogram(
        name="flow.entry_to_terminal.duration",
        unit="s",
        description="Wall-clock duration from flow entry to terminal state, in seconds.",
    )

    # Flow entry counter (throughput)
    _flow_entry_counter = _meter.create_counter(
        name="flow.entries",
        unit="{flow}",
        description="Number of times the primary business flow entry point was invoked.",
    )

    # Flow validation outcomes
    _flow_validation_counter = _meter.create_counter(
        name="flow.validation.outcomes",
        unit="{check}",
        description="Outcomes of per-step validation checks within the primary flow.",
    )

    # --- FastAPI auto-instrumentation (emits http.server.request.duration) ---
    FastAPIInstrumentor.instrument_app(
        app,
        tracer_provider=tracer_provider,
        meter_provider=meter_provider,
    )

    # --- Saturation middleware ---
    app.add_middleware(_ActiveRequestsMiddleware)


# ---------------------------------------------------------------------------
# Helpers for call-sites in other modules
# ---------------------------------------------------------------------------

def record_auth_attempt(outcome: str, reason: str = "") -> None:
    """Record one auth attempt.

    outcome: "success" | "denied"
    reason:  "expired" | "invalid_signature" | "wrong_password" | "" etc.
    """
    if _auth_attempts_counter is None:
        return
    attrs = {"outcome": outcome}
    if reason:
        attrs["reason"] = reason
    _auth_attempts_counter.add(1, attrs)


def record_flow_outcome(outcome: str, flow: str = "registration_to_publish") -> None:
    """Record a terminal outcome for the primary business flow.

    outcome: "success" | "failure"
    """
    if _flow_outcomes_counter is None:
        return
    _flow_outcomes_counter.add(1, {"flow": flow, "outcome": outcome})


def record_flow_entry(flow: str = "registration_to_publish") -> None:
    """Increment the flow-entry counter (throughput signal)."""
    if _flow_entry_counter is None:
        return
    _flow_entry_counter.add(1, {"flow": flow})


def record_flow_duration(elapsed_seconds: float, terminal_state: str, flow: str = "registration_to_publish") -> None:
    """Record entry-to-terminal wall-clock duration."""
    if _flow_duration_histogram is None:
        return
    _flow_duration_histogram.record(
        elapsed_seconds,
        {"flow": flow, "terminal_state": terminal_state},
    )


def record_flow_validation(step: str, outcome: str, flow: str = "registration_to_publish") -> None:
    """Record a per-step validation outcome.

    outcome: "passed" | "failed"
    """
    if _flow_validation_counter is None:
        return
    _flow_validation_counter.add(1, {"flow": flow, "step": step, "outcome": outcome})
