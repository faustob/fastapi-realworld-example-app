import os
import time

from fastapi import FastAPI, Request
from opentelemetry import metrics, trace
from opentelemetry.trace import Status, StatusCode
from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
    OTLPMetricExporter,
)
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource  # type: ignore[attr-defined]
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from starlette.middleware.base import BaseHTTPMiddleware

_SDK_INITIALIZED = False


def _init_sdk() -> None:
    global _SDK_INITIALIZED
    if _SDK_INITIALIZED:
        return
    _SDK_INITIALIZED = True

    service_name = os.environ.get("OTEL_SERVICE_NAME", "fastapi-realworld-example-app")
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")

    resource = Resource.create({"service.name": service_name})

    try:
        trace_exporter = (
            OTLPSpanExporter(endpoint=endpoint) if endpoint else OTLPSpanExporter()
        )
        tracer_provider = TracerProvider(resource=resource)
        tracer_provider.add_span_processor(BatchSpanProcessor(trace_exporter))
        trace.set_tracer_provider(tracer_provider)
    except Exception:
        pass

    try:
        metric_exporter = (
            OTLPMetricExporter(endpoint=endpoint) if endpoint else OTLPMetricExporter()
        )
        metric_reader = PeriodicExportingMetricReader(metric_exporter)
        meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
        metrics.set_meter_provider(meter_provider)
    except Exception:
        pass


_init_sdk()

tracer = trace.get_tracer(__name__)
meter = metrics.get_meter(__name__)

# Request outcome counter for availability SLI
request_outcome_counter = meter.create_counter(
    "http.server.request.outcome",
    unit="1",
    description="Count of HTTP requests by route and outcome class",
)

# Auth attempt outcome counter for auth-failure-rate SLI
auth_attempts_counter = meter.create_counter(
    "auth.attempts",
    unit="1",
    description="Count of authentication attempts by outcome and reason",
)

# Per-tenant request rate counter for throughput SLI
tenant_request_counter = meter.create_counter(
    "http.server.requests_by_tenant",
    unit="1",
    description="Count of HTTP requests by tenant/api key",
)

# Active request in-flight gauge (up-down counter) for saturation SLI
active_requests_updowncounter = meter.create_up_down_counter(
    "http.server.active_requests",
    unit="1",
    description="Number of in-flight HTTP requests",
)

# Worker pool size observable gauge for saturation SLI
_worker_pool_size = int(os.environ.get("WEB_CONCURRENCY", "1"))


def _observe_worker_pool_size(options):
    yield metrics.Observation(_worker_pool_size, {})


meter.create_observable_gauge(
    "http.server.worker_pool.size",
    callbacks=[_observe_worker_pool_size],
    unit="1",
    description="Configured worker pool size",
)

# Flow-level instruments for the primary registration-to-publish business flow
flow_outcome_counter = meter.create_counter(
    "flow.outcomes",
    unit="1",
    description="Count of primary flow completions by outcome",
)

flow_entry_counter = meter.create_counter(
    "flow.entries",
    unit="1",
    description="Count of primary flow entry invocations",
)

flow_duration_histogram = meter.create_histogram(
    "flow.duration",
    unit="s",
    description="End-to-end duration of the primary business flow",
)

flow_validation_outcome_counter = meter.create_counter(
    "flow.validation.outcomes",
    unit="1",
    description="Count of flow validation step outcomes",
)

entry_to_terminal_histogram = meter.create_histogram(
    "flow.entry_to_terminal.duration",
    unit="s",
    description="Wall-clock time from flow entry to terminal state",
)

# P99 slow-request budget (seconds) used for slow-request span events
_P99_BUDGET_SECONDS = 0.75


class SaturationMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        active_requests_updowncounter.add(1)
        start = time.perf_counter()
        route_template = None
        try:
            response = await call_next(request)
        except Exception as exc:
            elapsed = time.perf_counter() - start
            try:
                route_obj = request.scope.get("route")
                if route_obj is not None and getattr(route_obj, "path", None):
                    route_template = route_obj.path
            except Exception:
                pass
            span = trace.get_current_span()
            span.set_attribute("error.type", type(exc).__name__)
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            if elapsed > _P99_BUDGET_SECONDS:
                span.add_event(
                    "slow_request",
                    {
                        "http.route": route_template or "UNKNOWN",
                        "duration.seconds": elapsed,
                        "budget.seconds": _P99_BUDGET_SECONDS,
                    },
                )
            request_outcome_counter.add(
                1,
                {
                    "http.route": route_template or "UNKNOWN",
                    "outcome": "error",
                },
            )
            active_requests_updowncounter.add(-1)
            raise

        elapsed = time.perf_counter() - start
        try:
            route_obj = request.scope.get("route")
            if route_obj is not None and getattr(route_obj, "path", None):
                route_template = route_obj.path
            else:
                route_template = None
        except Exception:
            route_template = None

        resolved_route = route_template or "UNKNOWN"
        outcome = "success" if response.status_code < 500 else "error"
        request_outcome_counter.add(
            1,
            {
                "http.route": resolved_route,
                "outcome": outcome,
                "http.response.status_code": response.status_code,
            },
        )

        tenant_key = request.headers.get("authorization", "anonymous")
        tenant_request_counter.add(
            1,
            {
                "http.route": resolved_route,
                "tenant": "authenticated" if tenant_key != "anonymous" else "anonymous",
            },
        )

        if elapsed > _P99_BUDGET_SECONDS:
            span = trace.get_current_span()
            span.add_event(
                "slow_request",
                {
                    "http.route": resolved_route,
                    "duration.seconds": elapsed,
                    "budget.seconds": _P99_BUDGET_SECONDS,
                },
            )

        active_requests_updowncounter.add(-1)
        return response


def setup_telemetry(application: FastAPI) -> None:
    FastAPIInstrumentor.instrument_app(application)
