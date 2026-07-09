"""OpenTelemetry SDK bootstrap and shared instruments for the Conduit API.

This module builds and registers the global TracerProvider and MeterProvider
exactly ONCE at process startup (see app/main.py), configured from the
OTEL_EXPORTER_OTLP_ENDPOINT environment variable. Downstream modules obtain
tracers/meters via `trace.get_tracer(__name__)` / `metrics.get_meter(__name__)`
or via the shared instrument getters below.
"""
import logging
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

logger = logging.getLogger(__name__)

_SERVICE_NAME = os.environ.get("OTEL_SERVICE_NAME", "fastapi-realworld-example-app")

_setup_lock = threading.Lock()
_is_setup = False

_request_outcome_counter = None
_auth_attempts_counter = None
_slow_request_counter = None
active_requests_gauge = None
worker_pool_size_gauge = None
flow_outcome_counter = None
flow_duration_histogram = None
flow_entry_counter = None
flow_validation_counter = None
flow_entry_to_terminal_histogram = None


def setup_telemetry() -> None:
    """Build and register the global OTel providers exactly once.

    Safe to call multiple times (e.g. under reload) and defensive against an
    already-registered global provider (for example if an OTel agent is
    attached at the deployment layer outside this repo).
    """
    global _is_setup
    global _request_outcome_counter, _auth_attempts_counter, _slow_request_counter
    global active_requests_gauge, worker_pool_size_gauge
    global flow_outcome_counter, flow_duration_histogram, flow_entry_counter
    global flow_validation_counter, flow_entry_to_terminal_histogram

    with _setup_lock:
        if _is_setup:
            return

        resource = Resource.create({"service.name": _SERVICE_NAME})
        otlp_endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")

        try:
            tracer_provider = TracerProvider(resource=resource)
            span_exporter = OTLPSpanExporter(endpoint=otlp_endpoint) if otlp_endpoint else OTLPSpanExporter()
            tracer_provider.add_span_processor(BatchSpanProcessor(span_exporter))
            trace.set_tracer_provider(tracer_provider)
        except Exception:  # pragma: no cover - defensive against pre-set global provider
            logger.warning("TracerProvider already set globally; using existing provider", exc_info=True)

        try:
            metric_exporter = OTLPMetricExporter(endpoint=otlp_endpoint) if otlp_endpoint else OTLPMetricExporter()
            metric_reader = PeriodicExportingMetricReader(metric_exporter)
            meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
            metrics.set_meter_provider(meter_provider)
        except Exception:  # pragma: no cover - defensive against pre-set global provider
            logger.warning("MeterProvider already set globally; using existing provider", exc_info=True)

        meter = metrics.get_meter(__name__)

        _request_outcome_counter = meter.create_counter(
            name="http.server.request.outcomes",
            unit="1",
            description="Count of HTTP requests by route and outcome class (success/error)",
        )
        _auth_attempts_counter = meter.create_counter(
            name="auth.attempts",
            unit="1",
            description="Count of authentication attempts by outcome and denial reason",
        )
        _slow_request_counter = meter.create_counter(
            name="http.server.slow_requests",
            unit="1",
            description="Count of requests exceeding the P99 latency budget, by route",
        )
        active_requests_gauge = meter.create_up_down_counter(
            name="http.server.active_requests",
            unit="1",
            description="Number of in-flight HTTP requests",
        )
        worker_pool_size_gauge = meter.create_up_down_counter(
            name="http.server.worker_pool.size",
            unit="1",
            description="Configured size of the worker pool",
        )
        flow_outcome_counter = meter.create_counter(
            name="flow.outcomes",
            unit="1",
            description="Terminal outcomes of the registration-to-publish business flow",
        )
        flow_duration_histogram = meter.create_histogram(
            name="flow.duration",
            unit="s",
            description="End-to-end duration of the registration-to-publish business flow",
        )
        flow_entry_counter = meter.create_counter(
            name="flow.entries",
            unit="1",
            description="Count of business flow entry-point invocations",
        )
        flow_validation_counter = meter.create_counter(
            name="flow.validation.outcomes",
            unit="1",
            description="Count of per-step flow validation outcomes (passed/failed)",
        )
        flow_entry_to_terminal_histogram = meter.create_histogram(
            name="flow.entry_to_terminal.duration",
            unit="s",
            description="Wall-clock duration between flow entry and terminal state transition",
        )

        _is_setup = True


def instrument_fastapi_app(application) -> None:
    """Apply the official FastAPI OTel instrumentation to the app instance."""
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    FastAPIInstrumentor.instrument_app(application)


def get_request_outcome_counter():
    return _request_outcome_counter


def get_auth_attempts_counter():
    return _auth_attempts_counter


def get_slow_request_counter():
    return _slow_request_counter
