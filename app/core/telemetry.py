"""OpenTelemetry SDK bootstrap and shared instrument definitions.

Call setup_telemetry(app) exactly once at application startup (from
get_application() in app/main.py).  All other modules obtain meters/tracers
via get_tracer() / get_meter() which delegate to the already-registered
global providers.
"""
import os
import time
from typing import Optional

from fastapi import FastAPI, Request, Response
from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource, SERVICE_NAME
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

# ---------------------------------------------------------------------------
# Internal state – set once by setup_telemetry()
# ---------------------------------------------------------------------------
_meter: Optional[metrics.Meter] = None
_tracer: Optional[trace.Tracer] = None

# Instruments (created after the MeterProvider is registered)
_active_requests_counter: Optional[metrics.UpDownCounter] = None
_auth_attempts_counter: Optional[metrics.Counter] = None
flow_outcomes_counter: Optional[metrics.Counter] = None
flow_duration_histogram: Optional[metrics.Histogram] = None
flow_entry_to_terminal_histogram: Optional[metrics.Histogram] = None
flow_validation_outcomes_counter: Optional[metrics.Counter] = None


def get_tracer() -> trace.Tracer:
    """Return the global tracer (no-op until setup_telemetry has been called)."""
    return trace.get_tracer(__name__)


def get_meter() -> metrics.Meter:
    """Return the global meter (no-op until setup_telemetry has been called)."""
    return metrics.get_meter(__name__)


def setup_telemetry(app: FastAPI) -> None:
    """Initialise the OTel SDK and instrument the FastAPI application.

    Must be called exactly once, before the application starts serving
    requests.  Reads OTEL_EXPORTER_OTLP_ENDPOINT from the environment;
    falls back to the default gRPC endpoint (localhost:4317) when unset.
    """
    global _meter, _tracer
    global _active_requests_counter, _auth_attempts_counter
    global flow_outcomes_counter, flow_duration_histogram
    global flow_entry_to_terminal_histogram, flow_validation_outcomes_counter

    service_name = os.environ.get("OTEL_SERVICE_NAME", "fastapi-realworld-example-app")
    otlp_endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")

    resource = Resource.create({SERVICE_NAME: service_name})

    # ---- Traces ----
    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=otlp_endpoint))
    )
    trace.set_tracer_provider(tracer_provider)

    # ---- Metrics ----
    metric_reader = PeriodicExportingMetricReader(
        OTLPMetricExporter(endpoint=otlp_endpoint)
    )
    meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
    metrics.set_meter_provider(meter_provider)

    # ---- FastAPI auto-instrumentation (emits http.server.request.duration) ----
    FastAPIInstrumentor.instrument_app(app)

    # ---- Shared instrument definitions ----
    _meter = metrics.get_meter(__name__)
    _tracer = trace.get_tracer(__name__)

    # HTTP Worker Pool Saturation — in-flight request gauge
    _active_requests_counter = _meter.create_up_down_counter(
        name="http.server.active_requests",
        unit="{request}",
        description="Number of HTTP requests currently being processed.",
    )

    # Authentication Failure Rate
    _auth_attempts_counter = _meter.create_counter(
        name="auth.attempts",
        unit="{attempt}",
        description="Total authentication attempts, tagged by outcome and denial reason.",
    )

    # E2E Business Flow — outcome counter
    flow_outcomes_counter = _meter.create_counter(
        name="flow.outcomes",
        unit="{flow}",
        description="Terminal outcomes for the primary registration-to-publish flow.",
    )

    # E2E Business Flow — end-to-end latency histogram
    flow_duration_histogram = _meter.create_histogram(
        name="flow.duration",
        unit="s",
        description="End-to-end duration of the primary business flow in seconds.",
    )

    # Flow Completion Freshness — entry-to-terminal histogram
    flow_entry_to_terminal_histogram = _meter.create_histogram(
        name="flow.entry_to_terminal.duration",
        unit="s",
        description="Wall-clock time from flow entry to terminal state transition.",
    )

    # Flow Validation Failure Rate
    flow_validation_outcomes_counter = _meter.create_counter(
        name="flow.validation.outcomes",
        unit="{check}",
        description="Per-step validation outcomes for the primary flow.",
    )

    # ---- Active-request middleware (saturation SLI) ----
    @app.middleware("http")
    async def active_requests_middleware(request: Request, call_next) -> Response:
        """Track in-flight requests for the HTTP Worker Pool Saturation SLI."""
        route = request.scope.get("path", request.url.path)
        attrs = {"http.request.method": request.method, "http.route": route}
        _active_requests_counter.add(1, attrs)
        try:
            response = await call_next(request)
        finally:
            _active_requests_counter.add(-1, attrs)
        return response
