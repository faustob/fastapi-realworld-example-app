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
from opentelemetry.semconv.resource import ResourceAttributes
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

logger = logging.getLogger(__name__)

_INITIALIZED = False


def setup_telemetry() -> None:
    """Build and register the global OTel TracerProvider and MeterProvider exactly once."""
    global _INITIALIZED
    if _INITIALIZED:
        return

    service_name = os.environ.get("OTEL_SERVICE_NAME", "fastapi-realworld-example-app")
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")

    if not endpoint:
        endpoint = "http://localhost:4318"
        logger.warning(
            "OTEL_EXPORTER_OTLP_ENDPOINT is not set; defaulting to %s so the SDK is still "
            "registered globally and instruments are not no-ops. Set "
            "OTEL_EXPORTER_OTLP_ENDPOINT to point at your real collector.",
            endpoint,
        )

    resource = Resource.create({ResourceAttributes.SERVICE_NAME: service_name})

    try:
        span_exporter = OTLPSpanExporter(endpoint=endpoint)
        tracer_provider = TracerProvider(resource=resource)
        tracer_provider.add_span_processor(BatchSpanProcessor(span_exporter))
        trace.set_tracer_provider(tracer_provider)
    except Exception:  # pragma: no cover - defensive registration
        logger.exception("Failed to initialize OTel tracer provider")

    try:
        metric_exporter = OTLPMetricExporter(endpoint=endpoint)
        metric_reader = PeriodicExportingMetricReader(metric_exporter)
        meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
        metrics.set_meter_provider(meter_provider)
    except Exception:  # pragma: no cover - defensive registration
        logger.exception("Failed to initialize OTel meter provider")

    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor  # noqa: F401
    except ImportError:  # pragma: no cover
        pass

    _INITIALIZED = True


def get_meter(name: str):
    return metrics.get_meter(name)


def get_tracer(name: str):
    return trace.get_tracer(name)
