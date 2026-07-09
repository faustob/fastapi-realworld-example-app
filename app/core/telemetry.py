"""OpenTelemetry SDK bootstrap and shared custom instruments.

This module builds and registers the global TracerProvider and MeterProvider
exactly once at application startup. It also exposes module-level instruments
(counters/histograms) used across the app for business-level SLIs that are not
covered by the automatic FastAPI HTTP instrumentation (auth outcomes,
flow-level success/latency, validation outcomes, worker-pool saturation).

Module-level `metrics.get_meter(...)` / `trace.get_tracer(...)` calls and the
instruments created from them are safe even though this module may be
imported before `setup_telemetry()` runs: the OTel API returns proxy objects
that re-bind to the real provider once it is registered.
"""

import os
import threading

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource  # type: ignore[attr-defined]
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

_setup_lock = threading.Lock()
_is_setup = False


def setup_telemetry(service_name: str = "fastapi-realworld-example-app") -> None:
    """Build and register the global OTel SDK providers exactly once.

    Safe to call multiple times (idempotent) and safe if a language agent or
    another part of the process already registered a provider: set_* calls in
    the OTel Python API log and keep the existing provider rather than raise.
    """
    global _is_setup
    with _setup_lock:
        if _is_setup:
            return

        endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")

        resource = Resource.create({"service.name": os.environ.get("OTEL_SERVICE_NAME", service_name)})

        tracer_provider = TracerProvider(resource=resource)
        span_exporter = OTLPSpanExporter(endpoint=endpoint) if endpoint else OTLPSpanExporter()
        tracer_provider.add_span_processor(BatchSpanProcessor(span_exporter))
        trace.set_tracer_provider(tracer_provider)

        metric_exporter = OTLPMetricExporter(endpoint=endpoint) if endpoint else OTLPMetricExporter()
        metric_reader = PeriodicExportingMetricReader(metric_exporter)
        meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
        metrics.set_meter_provider(meter_provider)

        _is_setup = True


tracer = trace.get_tracer(__name__)
meter = metrics.get_meter(__name__)

# --- Request outcome (availability) ---
request_outcome_counter = meter.create_counter(
    name="http.server.request.outcome",
    description="Count of HTTP requests labeled by route and outcome class",
    unit="1",
)

# --- Auth attempt outcome (auth failure rate SLI) ---
auth_attempts_counter = meter.create_counter(
    name="auth.attempts",
    description="Count of authentication attempts labeled by outcome and denial reason",
    unit="1",
)

# --- Saturation (worker pool) ---
active_requests_updown_counter = meter.create_up_down_counter(
    name="http.server.active_requests",
    description="Number of in-flight HTTP requests",
    unit="1",
)

worker_pool_size_updown_counter = meter.create_up_down_counter(
    name="http.server.worker_pool.size",
    description="Configured size of the worker pool",
    unit="1",
)

# --- Business flow (registration-to-publish) ---
flow_entry_counter = meter.create_counter(
    name="flow.entries",
    description="Count of times the primary business flow entry point was invoked",
    unit="1",
)

flow_outcome_counter = meter.create_counter(
    name="flow.outcomes",
    description="Count of primary business flow terminal outcomes",
    unit="1",
)

flow_duration_histogram = meter.create_histogram(
    name="flow.duration",
    description="End-to-end duration of the primary business flow",
    unit="s",
)

flow_entry_to_terminal_histogram = meter.create_histogram(
    name="flow.entry_to_terminal.duration",
    description="Wall-clock time between flow entry event and terminal state transition",
    unit="s",
)

# --- Validation outcomes ---
validation_outcome_counter = meter.create_counter(
    name="flow.validation.outcomes",
    description="Count of per-step flow validation outcomes",
    unit="1",
)
