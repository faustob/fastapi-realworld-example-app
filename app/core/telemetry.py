"""OpenTelemetry SDK bootstrap.

Call setup_telemetry() exactly once at process startup (before any
instrumented code runs).  The SDK is registered as the global provider
so that get_tracer/__name__ and get_meter/__name__ calls everywhere else
resolve to real implementations.

Configuration is driven entirely by environment variables:
  OTEL_SERVICE_NAME          – service name (default: fastapi-realworld-example-app)
  OTEL_EXPORTER_OTLP_ENDPOINT – OTLP gRPC/HTTP endpoint (default: http://localhost:4317)
  OTEL_EXPORTER_OTLP_PROTOCOL – grpc | http/protobuf (default: grpc)
"""
from __future__ import annotations

import os

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

_SERVICE_NAME = os.getenv("OTEL_SERVICE_NAME", "fastapi-realworld-example-app")


def setup_telemetry() -> None:
    """Build and register the global TracerProvider and MeterProvider."""
    resource = Resource.create({"service.name": _SERVICE_NAME})

    # ── Traces ────────────────────────────────────────────────────────────────
    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter())
    )
    trace.set_tracer_provider(tracer_provider)

    # ── Metrics ───────────────────────────────────────────────────────────────
    metric_reader = PeriodicExportingMetricReader(OTLPMetricExporter())
    meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
    metrics.set_meter_provider(meter_provider)
