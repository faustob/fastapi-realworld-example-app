"""OpenTelemetry SDK bootstrap and shared instruments for the Conduit API.

The global TracerProvider/MeterProvider are built exactly once at process
startup by calling `setup_telemetry()` from `app.main.get_application()`.
Instruments below are created at import time using `trace.get_tracer` /
`metrics.get_meter`; the OTel Python API returns proxy objects until a
provider is registered, and those proxies automatically rebind to the real
provider once `setup_telemetry()` runs, so creating them here (even before
startup executes) is safe and not a no-op.
"""
import logging
import os
from typing import Iterable

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
    OTLPMetricExporter,
)
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.metrics import CallbackOptions, Observation
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

logger = logging.getLogger(__name__)

SERVICE_NAME = os.environ.get("OTEL_SERVICE_NAME", "fastapi-realworld-example-app")

_initialized = False


def setup_telemetry() -> None:
    """Build and globally register the OTel SDK providers, once.

    The OTLP endpoint is taken from the standard OTEL_EXPORTER_OTLP_ENDPOINT
    env var (never hardcoded). Safe to call more than once, and safe under a
    process where a provider may already be registered (e.g. by an attached
    agent): `set_tracer_provider`/`set_meter_provider` log and keep the
    existing provider instead of raising.
    """
    global _initialized  # noqa: WPS420
    if _initialized:
        return

    resource = Resource.create({"service.name": SERVICE_NAME})

    try:
        tracer_provider = TracerProvider(resource=resource)
        tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        trace.set_tracer_provider(tracer_provider)
    except Exception:  # noqa: BLE001
        logger.exception("failed to initialize OpenTelemetry tracer provider")

    try:
        metric_reader = PeriodicExportingMetricReader(OTLPMetricExporter())
        meter_provider = MeterProvider(
            resource=resource,
            metric_readers=[metric_reader],
        )
        metrics.set_meter_provider(meter_provider)
    except Exception:  # noqa: BLE001
        logger.exception("failed to initialize OpenTelemetry meter provider")

    _initialized = True


tracer = trace.get_tracer("app")
meter = metrics.get_meter("app")

# --- HTTP worker pool saturation -------------------------------------------------

http_active_requests = meter.create_up_down_counter(
    name="http.server.active_requests",
    unit="{request}",
    description="Number of in-flight HTTP requests currently being served",
)


def _observe_worker_pool_size(options: CallbackOptions) -> Iterable[Observation]:
    pool_size = int(os.environ.get("WEB_CONCURRENCY", "1"))
    yield Observation(pool_size, {})


meter.create_observable_gauge(
    name="http.server.worker_pool.size",
    callbacks=[_observe_worker_pool_size],
    unit="{worker}",
    description="Configured Uvicorn worker pool size",
)

# --- Authentication failure rate -------------------------------------------------

auth_attempts_counter = meter.create_counter(
    name="auth.attempts",
    unit="{attempt}",
    description="Authentication attempts, labeled by outcome and denial reason",
)

# --- Primary flow (registration-to-publish) --------------------------------------

flow_entry_counter = meter.create_counter(
    name="flow.entry",
    unit="{flow}",
    description="Entries into the primary registration-to-publish business flow",
)

flow_outcomes_counter = meter.create_counter(
    name="flow.outcomes",
    unit="{flow}",
    description="Terminal outcomes of the primary business flow",
)

flow_duration_histogram = meter.create_histogram(
    name="flow.duration",
    unit="s",
    description="Duration of the primary business flow",
)

flow_validation_counter = meter.create_counter(
    name="flow.validation.outcomes",
    unit="{check}",
    description="Per-step validation outcomes within the primary business flow",
)

flow_entry_to_terminal_histogram = meter.create_histogram(
    name="flow.entry_to_terminal.duration",
    unit="s",
    description="Wall-clock time between flow entry and its terminal state",
)
