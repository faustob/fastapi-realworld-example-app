import os
import threading

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

_setup_lock = threading.Lock()
_is_setup = False


def setup_telemetry(service_name: str) -> None:
    """Build and globally register the OTel TracerProvider and MeterProvider.

    Idempotent and safe to call even if an external agent (or a previous
    call) has already registered a global provider.
    """
    global _is_setup

    with _setup_lock:
        if _is_setup:
            return

        otlp_endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")

        resource = Resource.create({"service.name": service_name})

        try:
            tracer_provider = TracerProvider(resource=resource)
            span_exporter_kwargs = {}
            if otlp_endpoint:
                span_exporter_kwargs["endpoint"] = otlp_endpoint
            span_exporter = OTLPSpanExporter(**span_exporter_kwargs)
            tracer_provider.add_span_processor(BatchSpanProcessor(span_exporter))
            trace.set_tracer_provider(tracer_provider)
        except Exception:
            # set_tracer_provider does not raise on re-registration, but guard
            # defensively in case an agent has already configured tracing.
            pass

        try:
            metric_exporter_kwargs = {}
            if otlp_endpoint:
                metric_exporter_kwargs["endpoint"] = otlp_endpoint
            metric_exporter = OTLPMetricExporter(**metric_exporter_kwargs)
            metric_reader = PeriodicExportingMetricReader(metric_exporter)
            meter_provider = MeterProvider(
                resource=resource, metric_readers=[metric_reader]
            )
            metrics.set_meter_provider(meter_provider)
        except Exception:
            pass

        _is_setup = True
