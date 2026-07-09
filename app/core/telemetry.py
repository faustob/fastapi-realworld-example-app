"""OpenTelemetry bootstrap and shared instruments for the Conduit API.

This module builds and registers the global TracerProvider and
MeterProvider EXACTLY ONCE at process startup (invoked from app/main.py
before the FastAPI application is instrumented / begins serving
requests). It also exposes the shared meter/tracer and the custom
instruments used across the codebase (auth outcome counter, flow
outcome/duration instruments, active-request gauge, etc.).

Registration is defensive: if a TracerProvider/MeterProvider is already
registered (e.g. by an attached OTel agent), we log and continue using
the existing global provider instead of crashing on startup.
"""
import logging
import os
import time
from contextlib import contextmanager

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
    OTLPMetricExporter,
)
from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
    OTLPSpanExporter,
)
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

logger = logging.getLogger(__name__)

_TELEMETRY_INITIALIZED = False

_SERVICE_NAME = os.environ.get("OTEL_SERVICE_NAME", "fastapi-realworld-example-app")


def setup_telemetry() -> None:
    """Build and register the global OTel SDK providers exactly once.

    Safe to call multiple times (e.g. under test reload) and safe to run
    when an OTel agent has already registered global providers.
    """
    global _TELEMETRY_INITIALIZED
    if _TELEMETRY_INITIALIZED:
        return

    resource = Resource.create({"service.name": _SERVICE_NAME})

    otlp_endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")

    existing_tracer_provider = trace.get_tracer_provider()
    if isinstance(existing_tracer_provider, TracerProvider):
        logger.warning(
            "TracerProvider already configured (e.g. by an attached OTel agent); "
            "using existing global provider instead of registering a new one."
        )
    else:
        tracer_provider = TracerProvider(resource=resource)
        span_exporter = OTLPSpanExporter(endpoint=otlp_endpoint) if otlp_endpoint else OTLPSpanExporter()
        tracer_provider.add_span_processor(BatchSpanProcessor(span_exporter))
        trace.set_tracer_provider(tracer_provider)

    existing_meter_provider = metrics.get_meter_provider()
    if isinstance(existing_meter_provider, MeterProvider):
        logger.warning(
            "MeterProvider already configured (e.g. by an attached OTel agent); "
            "using existing global provider instead of registering a new one."
        )
    else:
        metric_exporter = OTLPMetricExporter(endpoint=otlp_endpoint) if otlp_endpoint else OTLPMetricExporter()
        metric_reader = PeriodicExportingMetricReader(metric_exporter)
        meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
        metrics.set_meter_provider(meter_provider)

    _TELEMETRY_INITIALIZED = True


def instrument_fastapi_app(application) -> None:
    """Attach the official FastAPI instrumentation to the app."""
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(application)
    except Exception as exc:  # pragma: no cover - defensive, never block startup
        logger.warning("FastAPI OTel instrumentation could not be attached: %s", exc)


def get_tracer():
    return trace.get_tracer("app")


def get_meter():
    return metrics.get_meter("app")


# ---------------------------------------------------------------------------
# Shared custom instruments (defined exactly once, recorded at call sites)
# ---------------------------------------------------------------------------

_meter = get_meter()

auth_attempts_counter = _meter.create_counter(
    name="auth.attempts",
    description="Count of authentication/authorization attempts by outcome",
    unit="1",
)

http_active_requests = _meter.create_up_down_counter(
    name="http.server.active_requests",
    description="Number of in-flight HTTP requests",
    unit="1",
)

http_worker_pool_size = _meter.create_up_down_counter(
    name="http.server.worker_pool.size",
    description="Configured size of the HTTP server worker pool",
    unit="1",
)

flow_outcomes_counter = _meter.create_counter(
    name="flow.outcomes",
    description="Terminal outcomes of the registration-to-publish primary flow",
    unit="1",
)

flow_entry_counter = _meter.create_counter(
    name="flow.entry",
    description="Count of primary-flow entry-point invocations",
    unit="1",
)

flow_duration_histogram = _meter.create_histogram(
    name="flow.duration",
    description="End-to-end duration of the primary business flow",
    unit="s",
)

flow_validation_outcomes_counter = _meter.create_counter(
    name="flow.validation.outcomes",
    description="Outcome of individual validation steps within the primary flow",
    unit="1",
)

entry_to_terminal_histogram = _meter.create_histogram(
    name="flow.entry_to_terminal.duration",
    description="Wall-clock duration between flow entry and terminal state",
    unit="s",
)


@contextmanager
def flow_span(name: str, flow_id: str):
    """Root span helper for a primary business flow, with entry/outcome counters.

    Usage:
        with flow_span("registration_to_publish", flow_id) as span:
            ... do work ...
            span.set_attribute("flow.outcome", "success")
    """
    tracer = get_tracer()
    start = time.monotonic()
    flow_entry_counter.add(1, {"flow": name})
    outcome = "success"
    with tracer.start_as_current_span(name) as span:
        span.set_attribute("flow.id", flow_id)
        try:
            yield span
        except Exception as exc:
            outcome = "failure"
            span.set_attribute("error.type", type(exc).__name__)
            raise
        finally:
            elapsed = time.monotonic() - start
            flow_duration_histogram.record(elapsed, {"flow": name, "outcome": outcome})
            flow_outcomes_counter.add(1, {"flow": name, "outcome": outcome})
