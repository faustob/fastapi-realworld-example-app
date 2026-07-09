"""OpenTelemetry SDK bootstrap and custom instruments for the Conduit API.

This module builds and registers the global TracerProvider and MeterProvider
exactly once at process startup (called from app.main.get_application),
and exposes counters/histograms/gauges used across the codebase for the
target SLIs (availability, latency, error rate, saturation, auth failures,
request throughput, and end-to-end business-flow metrics).
"""
import os
import threading
import time

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

_configured = False
_configure_lock = threading.Lock()

_active_requests = 0
_active_requests_lock = threading.Lock()

WORKER_POOL_SIZE = int(os.environ.get("WEB_CONCURRENCY", "1"))


def configure_telemetry(settings) -> None:
    """Build and register the global OTel providers exactly once.

    Safe to call multiple times (e.g. in tests) and tolerant of an
    already-registered provider (e.g. from an attached OTel agent).
    """
    global _configured
    with _configure_lock:
        if _configured:
            return

        otlp_endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
        service_name = os.environ.get("OTEL_SERVICE_NAME", getattr(settings, "project_name", "conduit-api"))

        resource = Resource.create({"service.name": service_name})

        try:
            tracer_provider = TracerProvider(resource=resource)
            span_exporter_kwargs = {}
            if otlp_endpoint:
                span_exporter_kwargs["endpoint"] = otlp_endpoint
            tracer_provider.add_span_processor(
                BatchSpanProcessor(OTLPSpanExporter(**span_exporter_kwargs))
            )
            trace.set_tracer_provider(tracer_provider)
        except Exception:
            # A provider may already be registered by an attached agent.
            pass

        try:
            metric_exporter_kwargs = {}
            if otlp_endpoint:
                metric_exporter_kwargs["endpoint"] = otlp_endpoint
            metric_reader = PeriodicExportingMetricReader(
                OTLPMetricExporter(**metric_exporter_kwargs)
            )
            meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
            metrics.set_meter_provider(meter_provider)
        except Exception:
            pass

        _configured = True


tracer = trace.get_tracer(__name__)
meter = metrics.get_meter(__name__)

# --- HTTP outcome / availability & error-rate ---
http_request_outcome_counter = meter.create_counter(
    "http.server.request.outcomes",
    unit="1",
    description="Count of HTTP requests labeled by route and outcome class",
)

# --- Auth failure rate ---
auth_attempts_counter = meter.create_counter(
    "auth.attempts",
    unit="1",
    description="Count of authentication attempts labeled by outcome and denial reason",
)

# --- Request throughput per tenant ---
http_requests_by_tenant_counter = meter.create_counter(
    "http.server.requests.by_tenant",
    unit="1",
    description="Count of HTTP requests labeled by tenant/api key",
)

# --- Saturation gauges ---
def _active_requests_callback(options):
    with _active_requests_lock:
        value = _active_requests
    yield metrics.Observation(value, {})


def _worker_pool_size_callback(options):
    yield metrics.Observation(WORKER_POOL_SIZE, {})


active_requests_gauge = meter.create_observable_gauge(
    "http.server.active_requests",
    callbacks=[_active_requests_callback],
    unit="1",
    description="Number of in-flight HTTP requests",
)

worker_pool_size_gauge = meter.create_observable_gauge(
    "http.server.worker_pool.size",
    callbacks=[_worker_pool_size_callback],
    unit="1",
    description="Configured size of the uvicorn worker pool",
)

# --- Business flow metrics (registration-to-publish primary flow) ---
flow_outcomes_counter = meter.create_counter(
    "flow.outcomes",
    unit="1",
    description="Terminal outcome count for the primary business flow",
)

flow_duration_histogram = meter.create_histogram(
    "flow.duration",
    unit="s",
    description="End-to-end duration of the primary business flow",
)

flow_validation_outcomes_counter = meter.create_counter(
    "flow.validation.outcomes",
    unit="1",
    description="Outcome count for per-step flow validation checks",
)

entry_to_terminal_histogram = meter.create_histogram(
    "flow.entry_to_terminal.duration",
    unit="s",
    description="Wall-clock time between flow entry event and terminal state transition",
)


async def saturation_middleware(request, call_next):
    """Track in-flight request count for the worker-pool saturation SLI.

    Records outcome/tenant counters based on the response status, without
    altering control flow or swallowing exceptions.
    """
    global _active_requests
    with _active_requests_lock:
        _active_requests += 1
    try:
        response = await call_next(request)
    finally:
        with _active_requests_lock:
            _active_requests -= 1

    route = request.scope.get("route")
    route_template = route.path if route is not None else request.url.path
    status_code = response.status_code
    outcome = "success" if status_code < 500 else "error"

    http_request_outcome_counter.add(
        1,
        {
            "http.route": route_template,
            "http.request.method": request.method,
            "http.response.status_code": status_code,
            "outcome": outcome,
        },
    )

    tenant = request.headers.get("x-api-key") or "unknown"
    http_requests_by_tenant_counter.add(
        1,
        {
            "http.route": route_template,
            "tenant": tenant,
        },
    )

    return response
