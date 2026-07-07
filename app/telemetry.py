"""OpenTelemetry SDK bootstrap for fastapi-realworld-example-app.

Call setup_telemetry() exactly once at process startup (before any
instrumented code runs).  The OTLP endpoint is read from the standard
OTEL_EXPORTER_OTLP_ENDPOINT environment variable; it is never hardcoded.
"""
import os
import time
from opentelemetry import trace, metrics
from opentelemetry.sdk.resources import Resource, SERVICE_NAME
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter

_SERVICE_NAME = os.environ.get("OTEL_SERVICE_NAME", "fastapi-realworld-example-app")

_telemetry_initialized = False


def setup_telemetry() -> None:
    """Build and register the global TracerProvider and MeterProvider.

    Safe to call multiple times — subsequent calls are no-ops.
    """
    global _telemetry_initialized
    if _telemetry_initialized:
        return
    _telemetry_initialized = True

    resource = Resource.create({SERVICE_NAME: _SERVICE_NAME})

    # --- Traces ---
    span_exporter = OTLPSpanExporter()
    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(BatchSpanProcessor(span_exporter))
    trace.set_tracer_provider(tracer_provider)

    # --- Metrics ---
    metric_exporter = OTLPMetricExporter()
    metric_reader = PeriodicExportingMetricReader(metric_exporter)
    meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
    metrics.set_meter_provider(meter_provider)


def get_meter(name: str):
    """Return a Meter for the given instrumentation scope."""
    return metrics.get_meter(name)


def get_tracer(name: str):
    """Return a Tracer for the given instrumentation scope."""
    return trace.get_tracer(name)
