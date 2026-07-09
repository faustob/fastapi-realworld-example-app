import logging
import os

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
    OTLPMetricExporter,
)
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

logger = logging.getLogger(__name__)

_initialized = False

# Module-level tracer/meter — these bind to proxy objects until
# set_tracer_provider/set_meter_provider run, then re-bind automatically.
tracer = trace.get_tracer(__name__)
meter = metrics.get_meter(__name__)

http_request_duration = meter.create_histogram(
    name="http.server.request.duration",
    unit="s",
    description="Duration of inbound HTTP requests",
)

request_outcome_counter = meter.create_counter(
    name="http.server.request.outcomes",
    unit="1",
    description="Count of HTTP requests labeled by route and outcome class",
)

active_requests = meter.create_up_down_counter(
    name="http.server.active_requests",
    unit="1",
    description="Number of in-flight HTTP requests",
)

auth_attempts_counter = meter.create_counter(
    name="auth.attempts",
    unit="1",
    description="Count of authentication attempts labeled by outcome and reason",
)

flow_outcomes_counter = meter.create_counter(
    name="flow.outcomes",
    unit="1",
    description="Count of primary business flow (registration-to-publish) terminal outcomes",
)

flow_duration_histogram = meter.create_histogram(
    name="flow.duration",
    unit="s",
    description="End-to-end duration of the primary business flow",
)

flow_entry_counter = meter.create_counter(
    name="flow.entries",
    unit="1",
    description="Count of primary business flow entries",
)

flow_validation_outcomes_counter = meter.create_counter(
    name="flow.validation.outcomes",
    unit="1",
    description="Count of per-step flow validation outcomes",
)

flow_entry_to_terminal_duration = meter.create_histogram(
    name="flow.entry_to_terminal.duration",
    unit="s",
    description="Wall-clock time between flow entry event and terminal state transition",
)


def setup_telemetry(settings) -> None:
    """Build and register the global TracerProvider/MeterProvider exactly once.

    Defensive: tolerates an already-registered provider (e.g. from an
    externally attached agent) since set_tracer_provider/set_meter_provider
    log and keep the existing provider rather than raising.
    """
    global _initialized
    if _initialized:
        return

    otlp_endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    service_name = os.environ.get("OTEL_SERVICE_NAME", getattr(settings, "project_name", "fastapi-realworld-example-app"))

    resource = Resource.create({"service.name": service_name})

    try:
        tracer_provider = TracerProvider(resource=resource)
        span_exporter = OTLPSpanExporter(endpoint=otlp_endpoint) if otlp_endpoint else OTLPSpanExporter()
        tracer_provider.add_span_processor(BatchSpanProcessor(span_exporter))
        trace.set_tracer_provider(tracer_provider)

        metric_exporter = OTLPMetricExporter(endpoint=otlp_endpoint) if otlp_endpoint else OTLPMetricExporter()
        metric_reader = PeriodicExportingMetricReader(metric_exporter)
        meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
        metrics.set_meter_provider(meter_provider)
    except Exception:  # pragma: no cover - defensive against double-init/agent conflicts
        logger.exception("Failed to initialize OpenTelemetry SDK; continuing with existing/no-op provider")

    _initialized = True
