"""OpenTelemetry SDK bootstrap and shared instruments for the Conduit API.

This module builds and registers the global TracerProvider and MeterProvider
EXACTLY ONCE at application startup (invoked from app/main.py's
get_application()). Do not call setup_telemetry() more than once per process.
Downstream code should obtain tracers/meters via get_tracer()/get_meter() or
the already-created instrument objects exported from this module.
"""
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


def setup_telemetry(service_name: str = "conduit-api") -> None:
    """Build and register the global TracerProvider/MeterProvider exactly once.

    Registration is guarded so that if an OTel agent or another part of the
    process already registered providers, we tolerate that instead of
    crashing the app at startup.
    """
    global _TELEMETRY_INITIALIZED
    if _TELEMETRY_INITIALIZED:
        return

    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")

    resource = Resource.create({"service.name": os.environ.get("OTEL_SERVICE_NAME", service_name)})

    try:
        span_exporter = OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces") if endpoint else OTLPSpanExporter()
        tracer_provider = TracerProvider(resource=resource)
        tracer_provider.add_span_processor(BatchSpanProcessor(span_exporter))
        trace.set_tracer_provider(tracer_provider)
    except Exception as exc:  # pragma: no cover - defensive registration
        logger.warning("Tracer provider already registered or failed to register: %s", exc)

    try:
        metric_exporter = OTLPMetricExporter(endpoint=f"{endpoint}/v1/metrics") if endpoint else OTLPMetricExporter()
        metric_reader = PeriodicExportingMetricReader(metric_exporter)
        meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
        metrics.set_meter_provider(meter_provider)
    except Exception as exc:  # pragma: no cover - defensive registration
        logger.warning("Meter provider already registered or failed to register: %s", exc)

    _TELEMETRY_INITIALIZED = True


def instrument_fastapi_app(app) -> None:
    """Apply the official FastAPI OTel instrumentation to emit
    http.server.request.duration with standard semconv attributes."""
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("FastAPI instrumentation could not be applied: %s", exc)


def get_tracer():
    return trace.get_tracer("app")


def get_meter():
    return metrics.get_meter("app")


# ---------------------------------------------------------------------------
# Shared instruments used across the request lifecycle, auth, and business
# flow instrumentation. Instruments are created lazily against the GLOBAL
# meter provider so they work whether or not an agent has already registered
# providers before setup_telemetry() runs.
# ---------------------------------------------------------------------------
_meter = metrics.get_meter("app")

request_outcome_counter = _meter.create_counter(
    name="http.server.request.outcomes",
    unit="1",
    description="Count of HTTP requests labeled by route and outcome class (success/4xx/5xx)",
)

ACTIVE_REQUESTS = _meter.create_up_down_counter(
    name="http.server.active_requests",
    unit="1",
    description="Number of in-flight HTTP requests",
)

worker_pool_size_gauge = _meter.create_observable_gauge(
    name="http.server.worker_pool.size",
    callbacks=[lambda options: [metrics.Observation(int(os.environ.get("WEB_CONCURRENCY", "1")))]],
    unit="1",
    description="Configured Uvicorn/Gunicorn worker pool size",
)

auth_attempts_counter = _meter.create_counter(
    name="auth.attempts",
    unit="1",
    description="Count of authentication/authorization attempts labeled by outcome and reason",
)

flow_outcomes_counter = _meter.create_counter(
    name="flow.outcomes",
    unit="1",
    description="Terminal outcome count for the registration-to-publish business flow",
)

flow_entry_counter = _meter.create_counter(
    name="flow.entries",
    unit="1",
    description="Count of entries into the registration-to-publish business flow",
)

flow_duration_histogram = _meter.create_histogram(
    name="flow.duration",
    unit="s",
    description="End-to-end duration of the registration-to-publish business flow",
)

flow_validation_outcomes_counter = _meter.create_counter(
    name="flow.validation.outcomes",
    unit="1",
    description="Outcome count for each validation step within the business flow",
)

entry_to_terminal_histogram = _meter.create_histogram(
    name="flow.entry_to_terminal.duration",
    unit="s",
    description="Wall-clock duration between a flow's entry event and its terminal state transition",
)


def active_requests_gauge():
    """Backwards-compat accessor placeholder for the active requests instrument."""
    return ACTIVE_REQUESTS
