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

_setup_lock = threading.Lock()
_is_setup = False


def setup_telemetry() -> None:
    """Build and register the OpenTelemetry SDK global providers exactly once.

    Safe to call multiple times (e.g. multiple app instances/tests) and safe
    if a language agent has already registered a provider before this code
    runs — registration errors from an already-set global are tolerated.
    """
    global _is_setup
    with _setup_lock:
        if _is_setup:
            return

        service_name = os.environ.get("OTEL_SERVICE_NAME", "fastapi-realworld-example-app")
        otlp_endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")

        resource = Resource.create({"service.name": service_name})

        try:
            tracer_provider = TracerProvider(resource=resource)
            span_exporter_kwargs = {}
            if otlp_endpoint:
                span_exporter_kwargs["endpoint"] = otlp_endpoint
            tracer_provider.add_span_processor(
                BatchSpanProcessor(OTLPSpanExporter(**span_exporter_kwargs))
            )
            trace.set_tracer_provider(tracer_provider)
        except Exception:
            # A tracer provider may already be registered by an external agent.
            pass

        try:
            metric_exporter_kwargs = {}
            if otlp_endpoint:
                metric_exporter_kwargs["endpoint"] = otlp_endpoint
            metric_reader = PeriodicExportingMetricReader(
                OTLPMetricExporter(**metric_exporter_kwargs)
            )
            meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
            metrics.set_meter_provider(meter_provider)
        except Exception:
            # A meter provider may already be registered by an external agent.
            pass

        _is_setup = True


def get_tracer():
    return trace.get_tracer(__name__)


def get_meter():
    return metrics.get_meter(__name__)
