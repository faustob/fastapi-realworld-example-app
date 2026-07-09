import os
import threading

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

_lock = threading.Lock()
_initialized = False

# --- Application-level instruments (shared across the app) ---
_meter = None
request_outcome_counter = None
auth_attempts_counter = None
flow_outcomes_counter = None
flow_entry_counter = None
flow_validation_outcomes_counter = None
flow_duration_histogram = None
flow_entry_to_terminal_histogram = None
active_requests_updowncounter = None
worker_pool_size_gauge = None


def setup_telemetry(service_name: str) -> None:
    """Build and register the OpenTelemetry SDK exactly once at process startup.

    Endpoint is read from OTEL_EXPORTER_OTLP_ENDPOINT (never hardcoded).
    Defensive: tolerate an already-registered provider (e.g. an attached agent)
    instead of crashing the app.
    """
    global _initialized, _meter
    global request_outcome_counter, auth_attempts_counter
    global flow_outcomes_counter, flow_entry_counter, flow_validation_outcomes_counter
    global flow_duration_histogram, flow_entry_to_terminal_histogram
    global active_requests_updowncounter, worker_pool_size_gauge

    with _lock:
        if _initialized:
            return

        resource = Resource.create({"service.name": service_name})

        span_exporter = OTLPSpanExporter()
        tracer_provider = TracerProvider(resource=resource)
        tracer_provider.add_span_processor(BatchSpanProcessor(span_exporter))
        try:
            trace.set_tracer_provider(tracer_provider)
        except Exception:
            # Provider may already be set by an attached agent; keep going,
            # but the exporter/provider objects above were built successfully.
            pass

        metric_exporter = OTLPMetricExporter()
        metric_reader = PeriodicExportingMetricReader(metric_exporter)
        meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
        try:
            metrics.set_meter_provider(meter_provider)
        except Exception:
            # Provider may already be set by an attached agent; keep going,
            # but the exporter/reader/provider objects above were built successfully.
            pass

        _meter = metrics.get_meter(__name__)

        request_outcome_counter = _meter.create_counter(
            name="http.server.request.outcomes",
            unit="1",
            description="Count of HTTP requests labeled by route and outcome class",
        )

        auth_attempts_counter = _meter.create_counter(
            name="auth.attempts",
            unit="1",
            description="Count of authentication attempts labeled by outcome and denial reason",
        )

        flow_outcomes_counter = _meter.create_counter(
            name="flow.outcomes",
            unit="1",
            description="Terminal outcome count for the registration-to-publish business flow",
        )

        flow_entry_counter = _meter.create_counter(
            name="flow.entries",
            unit="1",
            description="Count of entries into the primary business flow",
        )

        flow_validation_outcomes_counter = _meter.create_counter(
            name="flow.validation.outcomes",
            unit="1",
            description="Per-step validation outcome count for the primary business flow",
        )

        flow_duration_histogram = _meter.create_histogram(
            name="flow.duration",
            unit="s",
            description="End-to-end duration of the primary business flow",
        )

        flow_entry_to_terminal_histogram = _meter.create_histogram(
            name="flow.entry_to_terminal.duration",
            unit="s",
            description="Wall-clock time between flow entry and terminal state transition",
        )

        active_requests_updowncounter = _meter.create_up_down_counter(
            name="http.server.active_requests",
            unit="1",
            description="Number of in-flight HTTP requests",
        )

        worker_pool_size = int(os.environ.get("WEB_CONCURRENCY", "1"))

        def _observe_worker_pool_size(options):
            yield metrics.Observation(worker_pool_size, {})

        worker_pool_size_gauge = _meter.create_observable_gauge(
            name="http.server.worker_pool.size",
            unit="1",
            description="Configured size of the HTTP worker pool",
            callbacks=[_observe_worker_pool_size],
        )

        _initialized = True


def get_meter():
    return metrics.get_meter(__name__)
