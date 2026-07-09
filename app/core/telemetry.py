import hashlib
import logging
import os
import time
from contextvars import ContextVar
from threading import Lock
from typing import Callable

from fastapi import FastAPI, Request, Response
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

_TELEMETRY_INITIALIZED = False

# Active in-flight request gauge value, updated by the middleware and read by
# the observable gauge callback registered below. Guarded by _active_requests_lock
# since multiple concurrent requests mutate it from different async tasks/threads.
_active_requests = 0
_active_requests_lock = Lock()


def _service_name() -> str:
    return os.environ.get("OTEL_SERVICE_NAME", "fastapi-realworld-example-app")


def _otlp_endpoint() -> str:
    return os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")


def setup_telemetry(app: FastAPI) -> None:
    """Build and register the global OTel SDK exactly once, then instrument
    the FastAPI application. Safe to call even if a provider (e.g. an OTel
    agent) is already registered.
    """
    global _TELEMETRY_INITIALIZED
    if _TELEMETRY_INITIALIZED:
        return

    resource = Resource.create({"service.name": _service_name()})
    endpoint = _otlp_endpoint()

    if isinstance(trace.get_tracer_provider(), TracerProvider):
        logger.warning("TracerProvider already registered; using existing global provider")
    else:
        tracer_provider = TracerProvider(resource=resource)
        span_exporter = OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces")
        tracer_provider.add_span_processor(BatchSpanProcessor(span_exporter))
        trace.set_tracer_provider(tracer_provider)

    if isinstance(metrics.get_meter_provider(), MeterProvider):
        logger.warning("MeterProvider already registered; using existing global provider")
    else:
        metric_exporter = OTLPMetricExporter(endpoint=f"{endpoint}/v1/metrics")
        metric_reader = PeriodicExportingMetricReader(metric_exporter)
        meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
        metrics.set_meter_provider(meter_provider)

    FastAPIInstrumentor.instrument_app(app)

    _init_instruments()
    _register_saturation_gauges(app)
    _install_slow_request_middleware(app)

    _TELEMETRY_INITIALIZED = True


# ---------------------------------------------------------------------------
# Custom instruments (business + auth + flow SLIs)
# ---------------------------------------------------------------------------

request_outcome_counter = None
auth_attempts_counter = None
flow_outcomes_counter = None
flow_entry_counter = None
flow_duration_histogram = None
flow_validation_outcomes_counter = None
flow_entry_to_terminal_histogram = None
request_rate_counter = None

_P99_BUDGET_SECONDS = 0.75


def _init_instruments() -> None:
    global request_outcome_counter, auth_attempts_counter, flow_outcomes_counter
    global flow_entry_counter, flow_duration_histogram, flow_validation_outcomes_counter
    global flow_entry_to_terminal_histogram, request_rate_counter

    meter = metrics.get_meter(__name__)

    request_outcome_counter = meter.create_counter(
        name="http.server.request.outcomes",
        unit="1",
        description="Count of HTTP requests labeled by route and outcome class",
    )

    auth_attempts_counter = meter.create_counter(
        name="auth.attempts",
        unit="1",
        description="Count of authentication attempts labeled by outcome and denial reason",
    )

    flow_outcomes_counter = meter.create_counter(
        name="flow.outcomes",
        unit="1",
        description="Terminal outcomes of the primary registration-to-publish business flow",
    )

    flow_entry_counter = meter.create_counter(
        name="flow.entries",
        unit="1",
        description="Count of entries into the primary business flow",
    )

    flow_duration_histogram = meter.create_histogram(
        name="flow.duration",
        unit="s",
        description="End-to-end duration of the primary business flow",
    )

    flow_validation_outcomes_counter = meter.create_counter(
        name="flow.validation.outcomes",
        unit="1",
        description="Per-step validation outcomes within the primary business flow",
    )

    flow_entry_to_terminal_histogram = meter.create_histogram(
        name="flow.entry_to_terminal.duration",
        unit="s",
        description="Wall-clock duration between flow entry and terminal state transition",
    )

    request_rate_counter = meter.create_counter(
        name="http.server.requests.by_tenant",
        unit="1",
        description="Count of HTTP requests broken out by tenant/API key",
    )


def _register_saturation_gauges(app: FastAPI) -> None:
    meter = metrics.get_meter(__name__)

    def _active_requests_callback(options):
        with _active_requests_lock:
            current = _active_requests
        yield metrics.Observation(current, {})

    meter.create_observable_gauge(
        name="http.server.active_requests",
        callbacks=[_active_requests_callback],
        unit="1",
        description="Number of in-flight HTTP requests",
    )

    def _worker_pool_size_callback(options):
        pool_size = int(os.environ.get("WEB_CONCURRENCY", "1"))
        yield metrics.Observation(pool_size, {})

    meter.create_observable_gauge(
        name="http.server.worker_pool.size",
        callbacks=[_worker_pool_size_callback],
        unit="1",
        description="Configured size of the Uvicorn worker pool",
    )


def _install_slow_request_middleware(app: FastAPI) -> None:
    @app.middleware("http")
    async def _saturation_and_slow_request_middleware(request: Request, call_next: Callable) -> Response:
        global _active_requests
        with _active_requests_lock:
            _active_requests += 1
        start = time.time()
        response = None
        exc_raised = None
        try:
            response = await call_next(request)
        except Exception as exc:
            exc_raised = exc
            raise
        finally:
            with _active_requests_lock:
                _active_requests -= 1

            elapsed = time.time() - start
            if elapsed > _P99_BUDGET_SECONDS:
                span = trace.get_current_span()
                span.add_event(
                    "slow_request",
                    {
                        "http.route": _route_template(request),
                        "duration.seconds": elapsed,
                        "budget.seconds": _P99_BUDGET_SECONDS,
                    },
                )

            route = _route_template(request)
            if exc_raised is not None:
                status_code = 500
                outcome = "failure"
            else:
                status_code = response.status_code
                outcome = "success" if status_code < 500 else "failure"

            request_outcome_counter.add(
                1,
                {
                    "http.route": route,
                    "outcome": outcome,
                    "http.response.status_code": status_code,
                },
            )
            tenant_bucket = _tenant_bucket(request.headers.get("X-API-Key"))
            request_rate_counter.add(1, {"http.route": route, "tenant_bucket": tenant_bucket})

        return response


def _route_template(request: Request) -> str:
    route = request.scope.get("route")
    if route is not None and getattr(route, "path", None):
        return route.path
    return request.url.path


def _tenant_bucket(api_key: str | None) -> str:
    """Derive a low-cardinality bucket from an API key for metric attributes.

    Never place the raw API key value into a metric attribute: it is an
    unbounded, high-cardinality value that would explode the timeseries
    cardinality of the metrics backend. Hash it and bucket into a small,
    fixed number of buckets instead. The raw key may still be attached to
    spans (traces have much higher cardinality tolerance) if needed.
    """
    if not api_key:
        return "unknown"
    digest = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
    bucket = int(digest[:8], 16) % 16
    return f"bucket-{bucket}"
