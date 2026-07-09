"""OpenTelemetry SDK bootstrap and shared instruments for the Conduit API.

This module builds and registers the global TracerProvider and MeterProvider
exactly once at process startup (invoked from app.main.get_application), and
exposes the tracer/meter and custom instruments used across the app for the
service's target SLIs (availability, latency, error-rate, auth, flow metrics).

The OTLP endpoint is taken from the standard OTEL_EXPORTER_OTLP_ENDPOINT env
var (and related OTEL_* env vars); nothing is hardcoded here.
"""
import logging
import os
import threading

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
    OTLPSpanExporter,
)
from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
    OTLPMetricExporter,
)
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource  # type: ignore[attr-defined]
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

logger = logging.getLogger(__name__)

_setup_lock = threading.Lock()
_is_setup = False

SERVICE_NAME = os.environ.get("OTEL_SERVICE_NAME", "fastapi-realworld-example-app")


def setup_telemetry() -> None:
    """Build and register the global TracerProvider/MeterProvider exactly once.

    Defensive: if an SDK/agent has already registered global providers
    (e.g. via an attached OTel agent), tolerate it and just reuse the
    already-registered providers instead of crashing the app.
    """
    global _is_setup
    with _setup_lock:
        if _is_setup:
            return

        resource = Resource.create({"service.name": SERVICE_NAME})

        try:
            existing_tracer_provider = trace.get_tracer_provider()
            if not isinstance(existing_tracer_provider, TracerProvider):
                tracer_provider = TracerProvider(resource=resource)
                tracer_provider.add_span_processor(
                    BatchSpanProcessor(OTLPSpanExporter())
                )
                trace.set_tracer_provider(tracer_provider)
            else:
                logger.info("TracerProvider already registered; reusing it.")
        except Exception:  # pragma: no cover - defensive guard
            logger.warning("Could not set global TracerProvider; it may already be set.", exc_info=True)

        try:
            existing_meter_provider = metrics.get_meter_provider()
            if not isinstance(existing_meter_provider, MeterProvider):
                metric_reader = PeriodicExportingMetricReader(OTLPMetricExporter())
                meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
                metrics.set_meter_provider(meter_provider)
            else:
                logger.info("MeterProvider already registered; reusing it.")
        except Exception:  # pragma: no cover - defensive guard
            logger.warning("Could not set global MeterProvider; it may already be set.", exc_info=True)

        _is_setup = True


tracer = trace.get_tracer(__name__)
meter = metrics.get_meter(__name__)

# --- Custom instruments for SLIs without a direct auto-instrumentation signal ---

# Request outcome counter: availability SLI (route + outcome class)
http_request_outcomes = meter.create_counter(
    name="http.server.request.outcomes",
    unit="1",
    description="Count of HTTP server requests by route and outcome class (success/client_error/server_error).",
)

# Auth attempt outcome counter: authentication failure rate SLI
auth_attempts = meter.create_counter(
    name="auth.attempts",
    unit="1",
    description="Count of authentication attempts tagged with outcome and denial reason.",
)

# Active in-flight requests gauge (up/down counter): saturation SLI
http_active_requests = meter.create_up_down_counter(
    name="http.server.active_requests",
    unit="1",
    description="Number of in-flight HTTP requests currently being processed.",
)

# Worker pool size gauge (observable): saturation SLI
def _observe_worker_pool_size(options):
    size = int(os.environ.get("WEB_CONCURRENCY", os.environ.get("UVICORN_WORKERS", "1")))
    yield metrics.Observation(size, {})


http_worker_pool_size = meter.create_observable_gauge(
    name="http.server.worker_pool.size",
    callbacks=[_observe_worker_pool_size],
    unit="1",
    description="Configured Uvicorn worker pool size.",
)

# Flow instruments: registration-to-publish primary business flow
flow_outcomes = meter.create_counter(
    name="flow.outcomes",
    unit="1",
    description="Terminal outcome count of the primary registration-to-publish business flow.",
)

flow_duration = meter.create_histogram(
    name="flow.duration",
    unit="s",
    description="End-to-end duration of the primary business flow.",
)

flow_validation_outcomes = meter.create_counter(
    name="flow.validation.outcomes",
    unit="1",
    description="Outcome count of per-step validation checks within the primary business flow.",
)

entry_to_terminal_duration = meter.create_histogram(
    name="flow.entry_to_terminal.duration",
    unit="s",
    description="Wall-clock duration between a flow's entry event and its terminal state transition.",
)
