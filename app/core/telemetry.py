"""OpenTelemetry SDK bootstrap.

Builds and registers the global TracerProvider and MeterProvider exactly
once at application startup. The OTLP endpoint is taken from the standard
OTEL_EXPORTER_OTLP_ENDPOINT environment variable (never hardcoded).
"""
import logging
import os
import threading

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
    OTLPMetricExporter,
)
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor\n

logger = logging.getLogger(__name__)

_setup_lock = threading.Lock()
_is_setup = False


def setup_telemetry(service_name: str) -> None:
    """Idempotently build and register the global OTel SDK providers.

    Safe to call even if an OTel agent already registered global providers:
    set_tracer_provider/set_meter_provider log and keep the existing
    provider rather than raising.
    """
    global _is_setup
    with _setup_lock:
        if _is_setup:
            return

        resource = Resource.create({SERVICE_NAME: service_name})

        try:
            tracer_provider = TracerProvider(resource=resource)
            tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
            trace.set_tracer_provider(tracer_provider)
        except Exception:  # noqa: BLE001 - defensive: tolerate agent-preconfigured provider
            logger.warning("TracerProvider already configured; continuing with existing provider", exc_info=True)

        try:
            metric_reader = PeriodicExportingMetricReader(OTLPMetricExporter())
            meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
            metrics.set_meter_provider(meter_provider)
        except Exception:  # noqa: BLE001 - defensive: tolerate agent-preconfigured provider
            logger.warning("MeterProvider already configured; continuing with existing provider", exc_info=True)

        _is_setup = True


# Module-level tracer/meter using the standard get_tracer/get_meter pattern.
# These are safe to create before set_tracer_provider/set_meter_provider run:
# the API returns proxy objects that re-bind to the real provider once
# registered.
tracer = trace.get_tracer(__name__)
meter = metrics.get_meter(__name__)

flow_outcomes_counter = meter.create_counter(
    name="flow.outcomes",
    description="Count of primary business flow completions by outcome",
    unit="1",
)

flow_entry_counter = meter.create_counter(
    name="flow.entries",
    description="Count of primary business flow entry invocations",
    unit="1",
)

flow_duration_histogram = meter.create_histogram(
    name="flow.duration",
    description="Duration of the end-to-end primary business flow",
    unit="s",
)

flow_validation_outcomes_counter = meter.create_counter(
    name="flow.validation.outcomes",
    description="Count of primary flow validation outcomes",
    unit="1",
)
