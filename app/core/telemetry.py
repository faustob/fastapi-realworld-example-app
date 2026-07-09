import logging
import os

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

logger = logging.getLogger(__name__)

_TELEMETRY_INITIALIZED = False

meter = metrics.get_meter("app.conduit")
tracer = trace.get_tracer("app.conduit")

request_outcomes = meter.create_counter(
    "http.server.request.outcomes",
    unit="1",
    description="Count of HTTP requests labeled by route and outcome class",
)

active_requests = meter.create_up_down_counter(
    "http.server.active_requests",
    unit="1",
    description="Number of in-flight HTTP requests",
)

auth_attempts = meter.create_counter(
    "auth.attempts",
    unit="1",
    description="Count of authentication attempts labeled by outcome and denial reason",
)

flow_outcomes = meter.create_counter(
    "flow.outcomes",
    unit="1",
    description="Count of primary business flow terminal outcomes",
)

flow_entries = meter.create_counter(
    "flow.entries",
    unit="1",
    description="Count of primary business flow entries",
)

flow_duration = meter.create_histogram(
    "flow.duration",
    unit="s",
    description="End-to-end duration of the primary business flow",
)

flow_validation_outcomes = meter.create_counter(
    "flow.validation.outcomes",
    unit="1",
    description="Count of per-step flow validation outcomes",
)

flow_entry_to_terminal_duration = meter.create_histogram(
    "flow.entry_to_terminal.duration",
    unit="s",
    description="Wall-clock duration between flow entry and terminal state transition",
)


def setup_telemetry(service_name: str = "conduit") -> None:
    global _TELEMETRY_INITIALIZED
    if _TELEMETRY_INITIALIZED:
        return

    otlp_endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")

    resource = Resource.create({"service.name": os.environ.get("OTEL_SERVICE_NAME", service_name)})

    try:
        tracer_provider = TracerProvider(resource=resource)
        span_exporter_kwargs = {}
        if otlp_endpoint:
            span_exporter_kwargs["endpoint"] = otlp_endpoint
        span_exporter = OTLPSpanExporter(**span_exporter_kwargs)
        tracer_provider.add_span_processor(BatchSpanProcessor(span_exporter))
        trace.set_tracer_provider(tracer_provider)
    except Exception:  # pragma: no cover
        logger.exception("Failed to initialize tracer provider; continuing with existing provider")

    try:
        metric_exporter_kwargs = {}
        if otlp_endpoint:
            metric_exporter_kwargs["endpoint"] = otlp_endpoint
        metric_exporter = OTLPMetricExporter(**metric_exporter_kwargs)
        metric_reader = PeriodicExportingMetricReader(metric_exporter)
        meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
        metrics.set_meter_provider(meter_provider)
    except Exception:  # pragma: no cover
        logger.exception("Failed to initialize meter provider; continuing with existing provider")

    _TELEMETRY_INITIALIZED = True
