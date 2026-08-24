"""OpenTelemetry SDK bootstrap and shared instruments for the Conduit API.

This module builds and registers the global TracerProvider/MeterProvider
exactly once at import time, and exposes `init_telemetry(app)` which the
application factory (`app.main.get_application`) calls to install the
FastAPI auto-instrumentation and the saturation middleware.

Instruments below are created at import time as module-level singletons.
The OTel Python API returns proxy objects (ProxyMeterProvider) that
automatically re-bind to the real provider once `set_meter_provider` runs,
so creating them here - before or after provider registration - is safe.
"""
import os
from typing import Iterable

from fastapi import FastAPI
from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (
    OTLPMetricExporter,
)
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
    OTLPSpanExporter,
)
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.metrics import CallbackOptions, Observation
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

_SERVICE_NAME = os.environ.get("OTEL_SERVICE_NAME", "fastapi-realworld-example-app")
_WORKER_POOL_SIZE = int(os.environ.get("WEB_CONCURRENCY", "1"))

_resource = Resource.create({SERVICE_NAME: _SERVICE_NAME})

_tracer_provider = TracerProvider(resource=_resource)
_tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
trace.set_tracer_provider(_tracer_provider)

_meter_provider = MeterProvider(
    resource=_resource,
    metric_readers=[PeriodicExportingMetricReader(OTLPMetricExporter())],
)
metrics.set_meter_provider(_meter_provider)

tracer = trace.get_tracer(__name__)
meter = metrics.get_meter(__name__)

# --- Authentication / authorization outcomes --------------------------------
auth_attempts_counter = meter.create_counter(
    "auth.attempts",
    unit="1",
    description="Count of authentication/authorization decisions, tagged by outcome and reason.",
)

# --- HTTP worker pool saturation ---------------------------------------------
http_active_requests = meter.create_up_down_counter(
    "http.server.active_requests",
    unit="{request}",
    description="Number of in-flight HTTP requests.",
)


def _worker_pool_size_callback(_: CallbackOptions) -> Iterable[Observation]:
    yield Observation(_WORKER_POOL_SIZE, {})


http_worker_pool_size = meter.create_observable_gauge(
    "http.server.worker_pool.size",
    callbacks=[_worker_pool_size_callback],
    unit="{worker}",
    description="Configured Uvicorn worker pool size.",
)

# --- Primary flow: registration -> publish -----------------------------------
flow_entry_counter = meter.create_counter(
    "flow.entries",
    unit="1",
    description="Count of primary-flow entries (user registration), independent of outcome.",
)
flow_outcome_counter = meter.create_counter(
    "flow.outcomes",
    unit="1",
    description="Terminal outcome of the primary registration-to-publish flow.",
)
flow_duration_histogram = meter.create_histogram(
    "flow.duration",
    unit="s",
    description="End-to-end duration of the primary registration-to-publish flow.",
)
flow_entry_to_terminal_histogram = meter.create_histogram(
    "flow.entry_to_terminal.duration",
    unit="s",
    description="Wall-clock time between the flow entry and its terminal state, in seconds.",
)
flow_validation_counter = meter.create_counter(
    "flow.validation.outcomes",
    unit="1",
    description="Outcome of request validation checks encountered while running the primary flow.",
)


async def _saturation_middleware(request, call_next):
    http_active_requests.add(1)
    try:
        return await call_next(request)
    finally:
        http_active_requests.add(-1)


def init_telemetry(app: FastAPI) -> None:
    """Install OTel instrumentation on `app`.

    Must be called exactly once from the application factory, before the
    app starts serving traffic. Adds:
      * FastAPI/ASGI auto-instrumentation, emitting the standard
        `http.server.request.duration` histogram (method/route/status).
      * An in-flight request UpDownCounter + worker-pool-size gauge for the
        HTTP saturation SLI.
    """
    FastAPIInstrumentor.instrument_app(app)
    app.middleware("http")(_saturation_middleware)
