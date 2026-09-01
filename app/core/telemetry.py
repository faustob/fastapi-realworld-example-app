import os

from fastapi import FastAPI
from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
    OTLPMetricExporter,
)
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

_TELEMETRY_INITIALIZED = False


def init_telemetry(app: FastAPI) -> None:
    """Build and globally register the OpenTelemetry SDK exactly once for
    this process, then instrument the FastAPI application for standard HTTP
    server telemetry (http.server.request.duration with method/route/status
    attributes).

    Idempotent and safe to call once at startup: `set_tracer_provider` /
    `set_meter_provider` log and keep the existing provider instead of
    raising when a provider (e.g. from an attached language agent) is
    already registered, so this tolerates running with or without an agent.

    The OTLP endpoint is read from the standard
    OTEL_EXPORTER_OTLP_ENDPOINT / OTEL_EXPORTER_OTLP_TRACES_ENDPOINT /
    OTEL_EXPORTER_OTLP_METRICS_ENDPOINT environment variables by the
    exporters themselves; nothing is hardcoded here.
    """
    global _TELEMETRY_INITIALIZED
    if _TELEMETRY_INITIALIZED:
        return

    resource = Resource.create(
        {"service.name": os.environ.get("OTEL_SERVICE_NAME", "conduit-api")},
    )

    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(tracer_provider)

    metric_reader = PeriodicExportingMetricReader(OTLPMetricExporter())
    meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
    metrics.set_meter_provider(meter_provider)

    FastAPIInstrumentor.instrument_app(app)

    _TELEMETRY_INITIALIZED = True


# Module-level tracer/meter: obtained from the OTel API at import time, these
# are proxy objects that automatically rebind to the real providers once
# `init_telemetry` registers them globally above (correct behavior for the
# Python OTel API — not a no-op).
tracer = trace.get_tracer("app.flow")
meter = metrics.get_meter("app.flow")

# Primary business flow (user registration -> first article published):
# throughput (flow-entry counter).
flow_entries_counter = meter.create_counter(
    name="flow.entries",
    unit="1",
    description="Number of times the primary business flow (registration to first article) was entered",
)

# Success rate (terminal outcome counter).
flow_outcomes_counter = meter.create_counter(
    name="flow.outcomes",
    unit="1",
    description="Terminal outcomes of the primary business flow, by outcome (success/failure)",
)

# Latency (end-to-end duration of the flow's terminal/publish step).
flow_duration_histogram = meter.create_histogram(
    name="flow.duration",
    unit="s",
    description="Duration of the primary business flow's terminal step, in seconds",
)

# Validation failure rate, broken down per validation step.
flow_validation_outcomes_counter = meter.create_counter(
    name="flow.validation.outcomes",
    unit="1",
    description="Validation outcomes for each step of the primary business flow, by outcome (passed/failed)",
)
