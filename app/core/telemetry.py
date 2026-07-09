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

# Meter is created at import time using the API surface; the underlying
# ProxyMeterProvider re-binds to the real provider once set_meter_provider runs.
_meter = metrics.get_meter(__name__)
_tracer = trace.get_tracer(__name__)

request_duration = _meter.create_histogram(
    name="http.server.request.duration",
    unit="s",
    description="Duration of the inbound HTTP request",
)

request_outcomes = _meter.create_counter(
    name="http.server.request.outcomes",
    unit="1",
    description="Count of HTTP requests by route and outcome class",
)

active_requests = _meter.create_up_down_counter(
    name="http.server.active_requests",
    unit="1",
    description="Number of in-flight HTTP requests",
)

auth_attempts = _meter.create_counter(
    name="auth.attempts",
    unit="1",
    description="Count of authentication attempts by outcome and denial reason",
)

flow_outcomes = _meter.create_counter(
    name="flow.outcomes",
    unit="1",
    description="Terminal outcomes of the registration-to-publish business flow",
)

flow_entries = _meter.create_counter(
    name="flow.entries",
    unit="1",
    description="Count of entries into the registration-to-publish business flow",
)

flow_duration = _meter.create_histogram(
    name="flow.duration",
    unit="s",
    description="End-to-end duration of the registration-to-publish business flow",
)

flow_validation_outcomes = _meter.create_counter(
    name="flow.validation.outcomes",
    unit="1",
    description="Per-step validation outcomes within the business flow",
)

flow_entry_to_terminal = _meter.create_histogram(
    name="flow.entry_to_terminal.duration",
    unit="s",
    description="Wall-clock time between flow entry event and terminal state transition",
)


def get_tracer():
    return trace.get_tracer(__name__)


def setup_telemetry(service_name: str) -> None:
    """Builds and registers the global TracerProvider/MeterProvider exactly once.

    Defensive: if a provider (e.g. an attached agent) is already registered,
    this is a no-op — set_tracer_provider/set_meter_provider log and keep the
    existing provider rather than raising.
    """
    global _initialized
    if _initialized:
        return

    otlp_endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")

    resource = Resource.create({"service.name": service_name})

    try:
        tracer_provider = TracerProvider(resource=resource)
        span_exporter = OTLPSpanExporter(endpoint=otlp_endpoint) if otlp_endpoint else OTLPSpanExporter()
        tracer_provider.add_span_processor(BatchSpanProcessor(span_exporter))
        trace.set_tracer_provider(tracer_provider)
    except Exception:
        logger.exception("Failed to initialize OTel tracer provider")

    try:
        metric_exporter = (
            OTLPMetricExporter(endpoint=otlp_endpoint) if otlp_endpoint else OTLPMetricExporter()
        )
        metric_reader = PeriodicExportingMetricReader(metric_exporter)
        meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
        metrics.set_meter_provider(meter_provider)
    except Exception:
        logger.exception("Failed to initialize OTel meter provider")

    _initialized = True
