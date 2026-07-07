"""OpenTelemetry SDK bootstrap and shared instruments.

Call setup_telemetry(app) once at application startup (from get_application()).
All other modules obtain meters/tracers via get_tracer/__name__ / get_meter/__name__
after this module has run.
"""
import os
import time
from typing import Callable

from fastapi import FastAPI, Request, Response
from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

# ---------------------------------------------------------------------------
# Internal state
# ---------------------------------------------------------------------------
_meter = None
_active_requests_counter = None
_auth_attempts_counter = None
_flow_outcomes_counter = None
_flow_duration_histogram = None
_flow_entry_to_terminal_histogram = None
_flow_validation_outcomes_counter = None


def _build_resource() -> Resource:
    service_name = os.environ.get("OTEL_SERVICE_NAME", "fastapi-realworld-example-app")
    return Resource.create({"service.name": service_name})


def setup_telemetry(app: FastAPI) -> None:
    """Initialise the OTel SDK and instrument the FastAPI application.

    Must be called exactly once, before the application starts serving requests.
    """
    global _meter, _active_requests_counter, _auth_attempts_counter
    global _flow_outcomes_counter, _flow_duration_histogram
    global _flow_entry_to_terminal_histogram, _flow_validation_outcomes_counter

    resource = _build_resource()

    # --- Traces ---
    otlp_span_exporter = OTLPSpanExporter()
    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(BatchSpanProcessor(otlp_span_exporter))
    trace.set_tracer_provider(tracer_provider)

    # --- Metrics ---
    otlp_metric_exporter = OTLPMetricExporter()
    metric_reader = PeriodicExportingMetricReader(otlp_metric_exporter)
    meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
    metrics.set_meter_provider(meter_provider)

    _meter = metrics.get_meter(__name__)

    # Active-request UpDownCounter (saturation SLI)
    _active_requests_counter = _meter.create_up_down_counter(
        name="http.server.active_requests",
        unit="{request}",
        description="Number of HTTP requests currently being processed.",
    )

    # Auth attempt outcome counter (auth-failure-rate SLI)
    _auth_attempts_counter = _meter.create_counter(
        name="auth.attempts",
        unit="{attempt}",
        description="Total authentication/authorisation decisions, tagged by outcome and reason.",
    )

    # E2E flow outcome counter (flow success-rate + throughput SLIs)
    _flow_outcomes_counter = _meter.create_counter(
        name="flow.outcomes",
        unit="{flow}",
        description="Terminal outcomes of the primary registration-to-publish flow.",
    )

    # E2E flow duration histogram (flow latency P95 SLI)
    _flow_duration_histogram = _meter.create_histogram(
        name="flow.duration",
        unit="s",
        description="End-to-end duration of the primary registration-to-publish flow.",
    )

    # Entry-to-terminal duration histogram (flow freshness SLI)
    _flow_entry_to_terminal_histogram = _meter.create_histogram(
        name="flow.entry_to_terminal.duration",
        unit="s",
        description="Wall-clock time from flow entry to terminal state transition.",
    )

    # Flow validation outcome counter (validation failure-rate SLI)
    _flow_validation_outcomes_counter = _meter.create_counter(
        name="flow.validation.outcomes",
        unit="{check}",
        description="Per-step validation outcomes for the primary flow.",
    )

    # --- FastAPI auto-instrumentation (emits http.server.request.duration) ---
    # Excluded URLs: health/readiness probes (low value, high cardinality)
    FastAPIInstrumentor.instrument_app(
        app,
        tracer_provider=tracer_provider,
        meter_provider=meter_provider,
    )

    # --- Active-request middleware (saturation SLI) ---
    @app.middleware("http")
    async def active_requests_middleware(request: Request, call_next: Callable) -> Response:
        _active_requests_counter.add(1, {"http.request.method": request.method})
        try:
            response = await call_next(request)
        finally:
            _active_requests_counter.add(-1, {"http.request.method": request.method})
        return response


# ---------------------------------------------------------------------------
# Public helpers — import these in route/handler modules
# ---------------------------------------------------------------------------

def get_tracer(name: str = __name__):
    """Return a tracer from the globally-registered TracerProvider."""
    return trace.get_tracer(name)


def get_meter(name: str = __name__):
    """Return a meter from the globally-registered MeterProvider."""
    return metrics.get_meter(name)


def record_auth_attempt(outcome: str, reason: str = "") -> None:
    """Record one authentication/authorisation decision.

    outcome: "success" | "denied"
    reason:  "expired" | "invalid_signature" | "wrong_password" | "" (success)
    """
    if _auth_attempts_counter is None:
        return
    attrs = {"outcome": outcome}
    if reason:
        attrs["reason"] = reason
    _auth_attempts_counter.add(1, attrs)


def record_flow_outcome(outcome: str, flow: str = "registration_to_publish") -> None:
    """Record a terminal outcome for the primary E2E flow.

    outcome: "success" | "failure"
    """
    if _flow_outcomes_counter is None:
        return
    _flow_outcomes_counter.add(1, {"outcome": outcome, "flow": flow})


def record_flow_duration(elapsed_seconds: float, outcome: str, flow: str = "registration_to_publish") -> None:
    """Record the end-to-end duration of the primary E2E flow."""
    if _flow_duration_histogram is None:
        return
    _flow_duration_histogram.record(elapsed_seconds, {"outcome": outcome, "flow": flow})


def record_flow_entry_to_terminal(elapsed_seconds: float, terminal_state: str, flow: str = "registration_to_publish") -> None:
    """Record wall-clock time from flow entry to terminal state transition."""
    if _flow_entry_to_terminal_histogram is None:
        return
    _flow_entry_to_terminal_histogram.record(
        elapsed_seconds,
        {"flow": flow, "terminal_state": terminal_state},
    )


def record_flow_validation_outcome(step: str, outcome: str, flow: str = "registration_to_publish") -> None:
    """Record a per-step validation outcome for the primary flow.

    outcome: "passed" | "failed"
    """
    if _flow_validation_outcomes_counter is None:
        return
    _flow_validation_outcomes_counter.add(1, {"step": step, "outcome": outcome, "flow": flow})
