import logging
import os
import time

from fastapi import FastAPI, Request
from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
    OTLPMetricExporter,
)
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

logger = logging.getLogger(__name__)

_SERVICE_NAME = os.environ.get("OTEL_SERVICE_NAME", "fastapi-realworld-example-app")
_OTLP_ENDPOINT = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")

_telemetry_initialized = False

# In-flight request gauge and worker pool size, exposed via observable gauges.
_active_requests = 0
_worker_pool_size = int(os.environ.get("WEB_CONCURRENCY", "1"))

# P99 latency budget (seconds) used for slow-request span events.
_P99_BUDGET_SECONDS = 0.75


def _active_requests_callback(options):
    yield metrics.Observation(_active_requests, {})


def _worker_pool_size_callback(options):
    yield metrics.Observation(_worker_pool_size, {})


def setup_telemetry(app: FastAPI) -> None:
    """Build and register the global OTel SDK (once) and instrument FastAPI."""
    global _telemetry_initialized
    if _telemetry_initialized:
        return

    resource = Resource.create({"service.name": _SERVICE_NAME})

    tracer_provider = TracerProvider(resource=resource)
    span_exporter = OTLPSpanExporter(endpoint=_OTLP_ENDPOINT) if _OTLP_ENDPOINT else OTLPSpanExporter()
    tracer_provider.add_span_processor(BatchSpanProcessor(span_exporter))
    trace.set_tracer_provider(tracer_provider)

    metric_exporter = OTLPMetricExporter(endpoint=_OTLP_ENDPOINT) if _OTLP_ENDPOINT else OTLPMetricExporter()
    metric_reader = PeriodicExportingMetricReader(metric_exporter)
    meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
    metrics.set_meter_provider(meter_provider)

    FastAPIInstrumentor.instrument_app(app)

    meter = metrics.get_meter(__name__)

    meter.create_observable_gauge(
        name="http.server.active_requests",
        callbacks=[_active_requests_callback],
        unit="{request}",
        description="Number of in-flight HTTP requests.",
    )
    meter.create_observable_gauge(
        name="http.server.worker_pool.size",
        callbacks=[_worker_pool_size_callback],
        unit="{worker}",
        description="Configured size of the worker pool.",
    )

    request_outcome_counter = meter.create_counter(
        name="http.server.request.outcome",
        unit="{request}",
        description="Count of HTTP requests labeled by route and outcome class.",
    )
    request_rate_counter = meter.create_counter(
        name="http.server.requests.by_tenant",
        unit="{request}",
        description="Count of HTTP requests labeled by tenant/API key.",
    )
    auth_attempts_counter = meter.create_counter(
        name="auth.attempts",
        unit="{attempt}",
        description="Count of authentication attempts labeled by outcome and denial reason.",
    )

    app.state.request_outcome_counter = request_outcome_counter
    app.state.request_rate_counter = request_rate_counter
    app.state.auth_attempts_counter = auth_attempts_counter

    @app.middleware("http")
    async def _telemetry_middleware(request: Request, call_next):
        global _active_requests
        _active_requests += 1
        start = time.monotonic()
        route_template = request.url.path
        status_code = 500
        error_type = None
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        except Exception as exc:
            error_type = type(exc).__name__
            span = trace.get_current_span()
            span.set_attribute("error.type", error_type)
            raise
        finally:
            elapsed = time.monotonic() - start
            _active_requests -= 1

            route = request.scope.get("route")
            if route is not None and getattr(route, "path", None):
                route_template = route.path

            outcome = "success" if status_code < 500 else "failure"
            attrs = {
                "http.route": route_template,
                "http.request.method": request.method,
                "outcome": outcome,
            }
            if error_type:
                attrs["error.type"] = error_type
            request_outcome_counter.add(1, attrs)

            tenant = request.headers.get("Authorization", "anonymous")
            tenant_id = "anonymous" if tenant == "anonymous" else "authenticated"
            request_rate_counter.add(
                1,
                {
                    "http.route": route_template,
                    "tenant": tenant_id,
                },
            )

            if elapsed > _P99_BUDGET_SECONDS:
                span = trace.get_current_span()
                span.add_event(
                    "slow_request",
                    {
                        "http.route": route_template,
                        "duration_s": elapsed,
                        "budget_s": _P99_BUDGET_SECONDS,
                    },
                )

    _telemetry_initialized = True
