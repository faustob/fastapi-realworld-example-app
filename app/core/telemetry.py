import os
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from starlette.responses import JSONResponse

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

_SERVICE_NAME = os.environ.get("OTEL_SERVICE_NAME", "fastapi-realworld-example-app")

_initialized = False

# --- flow-level instruments (business flow: registration-to-publish) ---
_meter = None
flow_entry_counter = None
flow_outcome_counter = None
flow_duration_histogram = None
flow_validation_counter = None
flow_entry_to_terminal_histogram = None
auth_attempts_counter = None
request_outcome_counter = None
active_requests_updowncounter = None
worker_pool_size_updowncounter = None

_WORKER_POOL_SIZE = int(os.environ.get("WEB_CONCURRENCY", "1"))


def _build_providers() -> None:
    resource = Resource.create({"service.name": _SERVICE_NAME})

    otlp_endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")

    # Tracing
    try:
        tracer_provider = TracerProvider(resource=resource)
        span_exporter = OTLPSpanExporter(endpoint=otlp_endpoint) if otlp_endpoint else OTLPSpanExporter()
        tracer_provider.add_span_processor(BatchSpanProcessor(span_exporter))
        trace.set_tracer_provider(tracer_provider)
    except Exception:
        # A global tracer provider may already be registered (e.g. by an agent).
        pass

    # Metrics
    try:
        metric_exporter = OTLPMetricExporter(endpoint=otlp_endpoint) if otlp_endpoint else OTLPMetricExporter()
        reader = PeriodicExportingMetricReader(metric_exporter)
        meter_provider = MeterProvider(resource=resource, metric_readers=[reader])
        metrics.set_meter_provider(meter_provider)
    except Exception:
        pass


def _build_instruments() -> None:
    global _meter
    global flow_entry_counter, flow_outcome_counter, flow_duration_histogram
    global flow_validation_counter, flow_entry_to_terminal_histogram
    global auth_attempts_counter, request_outcome_counter
    global active_requests_updowncounter, worker_pool_size_updowncounter

    _meter = metrics.get_meter(__name__)

    request_outcome_counter = _meter.create_counter(
        name="http.server.request.outcomes",
        unit="1",
        description="Count of HTTP requests labeled by route and outcome class",
    )

    auth_attempts_counter = _meter.create_counter(
        name="auth.attempts",
        unit="1",
        description="Count of authentication attempts labeled by outcome and reason",
    )

    active_requests_updowncounter = _meter.create_up_down_counter(
        name="http.server.active_requests",
        unit="{request}",
        description="Number of in-flight HTTP requests",
    )

    worker_pool_size_updowncounter = _meter.create_up_down_counter(
        name="http.server.worker_pool.size",
        unit="{worker}",
        description="Configured worker pool size",
    )
    worker_pool_size_updowncounter.add(_WORKER_POOL_SIZE)

    flow_entry_counter = _meter.create_counter(
        name="flow.entries.total",
        unit="1",
        description="Count of primary-flow entry invocations",
    )

    flow_outcome_counter = _meter.create_counter(
        name="flow.outcomes.total",
        unit="1",
        description="Count of primary-flow terminal outcomes",
    )

    flow_duration_histogram = _meter.create_histogram(
        name="flow.duration",
        unit="s",
        description="End-to-end duration of the primary business flow",
    )

    flow_validation_counter = _meter.create_counter(
        name="flow.validation.outcomes.total",
        unit="1",
        description="Count of per-step flow validation outcomes",
    )

    flow_entry_to_terminal_histogram = _meter.create_histogram(
        name="flow.entry_to_terminal.duration",
        unit="s",
        description="Wall-clock time between a flow's entry event and its terminal state transition",
    )


def setup_telemetry(app: FastAPI) -> None:
    global _initialized
    if _initialized:
        return
    _initialized = True

    _build_providers()
    _build_instruments()

    FastAPIInstrumentor.instrument_app(app)

    tracer = trace.get_tracer(__name__)

    @app.middleware("http")
    async def _saturation_and_outcome_middleware(request: Request, call_next):
        active_requests_updowncounter.add(1)
        start = time.monotonic()
        route_template = request.url.path
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            duration = time.monotonic() - start
            active_requests_updowncounter.add(-1)
            matched_route = request.scope.get("route")
            if matched_route is not None and hasattr(matched_route, "path"):
                route_template = matched_route.path
            outcome = "success" if status_code < 500 else "failure"
            request_outcome_counter.add(
                1,
                {
                    "http.route": route_template,
                    "http.request.method": request.method,
                    "outcome": outcome,
                    "http.response.status_code": status_code,
                },
            )
            if duration > 0.75:
                span = trace.get_current_span()
                span.add_event(
                    "slow_request",
                    {
                        "http.route": route_template,
                        "http.request.method": request.method,
                        "duration_s": duration,
                    },
                )

