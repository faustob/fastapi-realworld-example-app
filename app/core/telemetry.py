"""OpenTelemetry SDK bootstrap for the Conduit FastAPI application.

Builds and registers the global TracerProvider and MeterProvider exactly
once at process startup, exporting via OTLP to the endpoint configured by
the standard OTEL_EXPORTER_OTLP_ENDPOINT environment variable (never
hardcoded).
"""
import logging
import os
import time
from typing import Callable, Dict, Optional

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
    OTLPMetricExporter,
)
from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
    OTLPSpanExporter,
)
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource  # type: ignore[attr-defined]
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

logger = logging.getLogger(__name__)

_initialized = False

_meter = None

# Instruments, created lazily on first setup_telemetry() call and exposed
# via module-level getters so route/auth code can import and record to them.
http_outcome_counter = None
auth_attempts_counter = None
request_rate_counter = None
flow_outcome_counter = None
flow_duration_histogram = None
flow_entry_counter = None
flow_validation_counter = None
flow_freshness_histogram = None
active_requests_updowncounter = None
worker_pool_size_updowncounter = None


def setup_telemetry(service_name: str = "conduit-api") -> None:
    """Idempotently build and register global Tracer/Meter providers.

    Defensive: if a provider is already registered (e.g. by an attached
    OTel agent), this does not attempt to overwrite it — it just reuses
    the existing global provider.
    """
    global _initialized, _meter
    global http_outcome_counter, auth_attempts_counter, request_rate_counter
    global flow_outcome_counter, flow_duration_histogram, flow_entry_counter
    global flow_validation_counter, flow_freshness_histogram
    global active_requests_updowncounter, worker_pool_size_updowncounter

    if _initialized:
        return

    resource = Resource.create({"service.name": os.getenv("OTEL_SERVICE_NAME", service_name)})

    otlp_endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")

    # --- Tracing ---
    try:
        existing_tracer_provider = trace.get_tracer_provider()
        if isinstance(existing_tracer_provider, TracerProvider):
            tracer_provider = existing_tracer_provider
        else:
            tracer_provider = TracerProvider(resource=resource)
            span_exporter = OTLPSpanExporter(endpoint=otlp_endpoint) if otlp_endpoint else OTLPSpanExporter()
            tracer_provider.add_span_processor(BatchSpanProcessor(span_exporter))
            trace.set_tracer_provider(tracer_provider)
    except Exception:  # pragma: no cover - defensive against already-set provider
        logger.warning("TracerProvider already registered; reusing existing global provider", exc_info=True)

    # --- Metrics ---
    try:
        existing_meter_provider = metrics.get_meter_provider()
        if isinstance(existing_meter_provider, MeterProvider):
            meter_provider = existing_meter_provider
        else:
            metric_exporter = OTLPMetricExporter(endpoint=otlp_endpoint) if otlp_endpoint else OTLPMetricExporter()
            metric_reader = PeriodicExportingMetricReader(metric_exporter)
            meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
            metrics.set_meter_provider(meter_provider)
    except Exception:  # pragma: no cover - defensive against already-set provider
        logger.warning("MeterProvider already registered; reusing existing global provider", exc_info=True)

    _meter = metrics.get_meter(__name__)

    http_outcome_counter = _meter.create_counter(
        name="http.server.request.outcomes",
        unit="1",
        description="Count of HTTP requests labeled by route and outcome class",
    )
    auth_attempts_counter = _meter.create_counter(
        name="auth.attempts",
        unit="1",
        description="Count of authentication attempts labeled by outcome and denial reason",
    )
    request_rate_counter = _meter.create_counter(
        name="http.server.requests.total",
        unit="1",
        description="Total HTTP requests, labeled by tenant/api key",
    )
    flow_outcome_counter = _meter.create_counter(
        name="flow.outcomes",
        unit="1",
        description="Terminal outcome count of the registration-to-publish business flow",
    )
    flow_duration_histogram = _meter.create_histogram(
        name="flow.duration",
        unit="s",
        description="End-to-end duration of the registration-to-publish business flow",
    )
    flow_entry_counter = _meter.create_counter(
        name="flow.entries",
        unit="1",
        description="Count of flow entry-point invocations",
    )
    flow_validation_counter = _meter.create_counter(
        name="flow.validation.outcomes",
        unit="1",
        description="Count of per-step flow validation outcomes",
    )
    flow_freshness_histogram = _meter.create_histogram(
        name="flow.entry_to_terminal.duration",
        unit="s",
        description="Wall-clock duration between flow entry and terminal state transition",
    )
    active_requests_updowncounter = _meter.create_up_down_counter(
        name="http.server.active_requests",
        unit="1",
        description="Number of in-flight HTTP requests",
    )
    worker_pool_size_updowncounter = _meter.create_up_down_counter(
        name="http.server.worker_pool.size",
        unit="1",
        description="Configured size of the worker pool",
    )

    pool_size = int(os.getenv("WEB_CONCURRENCY", "1"))
    worker_pool_size_updowncounter.add(pool_size)

    _initialized = True


def get_meter():
    return _meter
