"""OpenTelemetry bootstrap for the Conduit API.

This module builds the TracerProvider and MeterProvider exactly once at
application startup (invoked from ``app.main.get_application``) and registers
them as the global OpenTelemetry SDK instances. Instruments defined at module
import time are safe to use even before ``setup_telemetry`` runs: the
OpenTelemetry API returns proxy objects that transparently re-bind once the
real providers are registered.
"""
import os
import time
from typing import Callable, Iterable

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.metrics import CallbackOptions, Observation
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Status, StatusCode
from starlette.applications import Starlette
from starlette.requests import Request

_TELEMETRY_INITIALIZED = False

# Aligned with the HTTP Response Time P99 budget (750 ms).
SLOW_REQUEST_THRESHOLD_SECONDS = 0.75

_tracer_provider_ref: TracerProvider | None = None
_meter_provider_ref: MeterProvider | None = None


async def shutdown_telemetry() -> None:
    """Flush and shut down the TracerProvider/MeterProvider on app shutdown.

    Registered as a FastAPI shutdown event handler so buffered spans/metrics
    held by the BatchSpanProcessor and PeriodicExportingMetricReader are
    exported before the process exits.
    """
    if _tracer_provider_ref is not None:
        _tracer_provider_ref.force_flush()
        _tracer_provider_ref.shutdown()
    if _meter_provider_ref is not None:
        _meter_provider_ref.force_flush()
        _meter_provider_ref.shutdown()


def setup_telemetry(app: Starlette, service_name: str = "conduit-api") -> None:
    """Build and register the OpenTelemetry SDK exactly once, then instrument FastAPI.

    The OTLP endpoint is taken from the standard ``OTEL_EXPORTER_OTLP_ENDPOINT``
    environment variable (read internally by the exporters) — never hardcoded.
    """
    global _TELEMETRY_INITIALIZED
    if _TELEMETRY_INITIALIZED:
        return

    resource = Resource.create({"service.name": os.environ.get("OTEL_SERVICE_NAME", service_name)})

    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    # set_tracer_provider logs and keeps the existing provider if one (e.g. an
    # attached agent) is already registered — safe to call unconditionally.
    trace.set_tracer_provider(tracer_provider)

    metric_reader = PeriodicExportingMetricReader(OTLPMetricExporter())
    meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
    metrics.set_meter_provider(meter_provider)

    global _tracer_provider_ref, _meter_provider_ref
    _tracer_provider_ref = tracer_provider
    _meter_provider_ref = meter_provider

    # NOTE: FastAPIInstrumentor is intentionally NOT used here to avoid double
    # recording http.server.request.duration / request counts: the custom
    # telemetry_middleware below already emits the standard HTTP request
    # duration histogram and outcome counters, plus app-specific concerns
    # (auth attempts, flow tracking, active requests).

    app.add_event_handler("shutdown", shutdown_telemetry)

    _TELEMETRY_INITIALIZED = True


_meter = metrics.get_meter(__name__)
_tracer = trace.get_tracer(__name__)

http_request_outcomes = _meter.create_counter(
    "http.server.request.count",
    unit="{request}",
    description="Count of completed HTTP requests labeled by route, method and outcome class",
)

http_request_duration = _meter.create_histogram(
    "http.server.request.duration",
    unit="s",
    description="Duration of HTTP server requests in seconds, labeled by route, method and status code",
)

auth_attempts = _meter.create_counter(
    "auth.attempts.count",
    unit="{attempt}",
    description="Count of authentication attempts labeled by outcome and denial reason",
)

active_requests = _meter.create_up_down_counter(
    "http.server.active_requests",
    unit="{request}",
    description="Number of in-flight HTTP requests",
)

flow_entry_counter = _meter.create_counter(
    "flow.entry.count",
    unit="{flow}",
    description="Count of registration-to-publish flow entries (new user registrations)",
)

flow_outcome_counter = _meter.create_counter(
    "flow.outcome.count",
    unit="{flow}",
    description="Count of registration-to-publish flow terminal outcomes",
)


def _worker_pool_size_callback(options: CallbackOptions) -> Iterable[Observation]:
    pool_size = int(os.environ.get("WEB_CONCURRENCY", "1"))
    yield Observation(pool_size, {})


worker_pool_size = _meter.create_observable_gauge(
    "http.server.worker_pool.size",
    callbacks=[_worker_pool_size_callback],
    unit="{worker}",
    description="Configured Uvicorn/Gunicorn worker pool size",
)


def _worker_pool_busy_callback(options: CallbackOptions) -> Iterable[Observation]:
    yield Observation(_active_request_count, {})


worker_pool_busy = _meter.create_observable_gauge(
    "http.server.worker_pool.busy",
    callbacks=[_worker_pool_busy_callback],
    unit="{worker}",
    description="Approximate number of workers currently busy handling an in-flight request, for computing worker pool saturation (busy/size)",
)

flow_validation_counter = _meter.create_counter(
    "flow.validation.count",
    unit="{check}",
    description="Count of registration-to-publish flow validation checks labeled by step and outcome (pass/fail)",
)

flow_duration_histogram = _meter.create_histogram(
    "flow.duration",
    unit="s",
    description="Duration in seconds from registration-to-publish flow entry to terminal outcome",
)

_active_request_count = 0
_flow_start_times: dict = {}  # type: ignore[type-arg]


async def telemetry_middleware(request: Request, call_next: Callable):
    """Record request-outcome, auth-attempt, flow and active-request telemetry.

    Wraps the existing request handling without altering control flow: on an
    unhandled exception it records telemetry and re-raises the SAME exception
    so downstream error handling (ServerErrorMiddleware, ASGI server logging)
    behaves exactly as before.
    """
    global _active_request_count
    method = request.method
    active_requests.add(1, {"http.request.method": method})
    _active_request_count += 1
    start = time.perf_counter()
    status_code = 500
    exception_type = None
    try:
        response = await call_next(request)
        status_code = response.status_code
        return response
    except Exception as exc:
        exception_type = type(exc).__name__
        span = trace.get_current_span()
        span.set_attribute("error.type", exception_type)
        span.set_status(Status(StatusCode.ERROR, str(exc)))
        raise
    finally:
        duration = time.perf_counter() - start
        active_requests.add(-1, {"http.request.method": method})
        _active_request_count -= 1

        route = request.scope.get("route")
        route_template = route.path if route is not None else request.url.path
        outcome = "success" if status_code < 500 else "error"

        attrs = {
            "http.route": route_template,
            "http.request.method": method,
            "http.response.status_code": status_code,
            "outcome": outcome,
        }
        if exception_type:
            attrs["error.type"] = exception_type
        http_request_outcomes.add(1, attrs)
        http_request_duration.record(
            duration,
            {
                "http.route": route_template,
                "http.request.method": method,
                "http.response.status_code": status_code,
            },
        )

        if duration > SLOW_REQUEST_THRESHOLD_SECONDS:
            trace.get_current_span().add_event(
                "slow_request",
                {"http.route": route_template, "duration_s": duration},
            )

        normalized_route = route_template.rstrip("/")

        if normalized_route.endswith("/users/login") and method == "POST":
            if status_code < 400:
                auth_attempts.add(1, {"outcome": "granted", "reason": "ok"})
            elif status_code == 401:
                auth_attempts.add(1, {"outcome": "denied", "reason": "invalid_credentials"})
            elif status_code == 403:
                auth_attempts.add(1, {"outcome": "denied", "reason": "forbidden"})
            else:
                auth_attempts.add(1, {"outcome": "denied", "reason": "server_error"})

        if normalized_route.endswith("/users") and method == "POST":
            flow_entry_counter.add(1, {"flow": "registration_to_publish"})
            validation_outcome = "pass" if status_code < 400 else "fail"
            flow_validation_counter.add(
                1, {"flow": "registration_to_publish", "step": "registration", "outcome": validation_outcome}
            )
            if status_code < 400:
                user_key = request.headers.get("authorization") or id(request)
                _flow_start_times[user_key] = start

        if normalized_route.endswith("/articles") and method == "POST":
            terminal_outcome = "success" if status_code < 400 else "failed"
            flow_outcome_counter.add(1, {"flow": "registration_to_publish", "outcome": terminal_outcome})
            validation_outcome = "pass" if status_code < 400 else "fail"
            flow_validation_counter.add(
                1, {"flow": "registration_to_publish", "step": "publish", "outcome": validation_outcome}
            )
            if status_code < 400:
                user_key = request.headers.get("authorization")
                flow_start = _flow_start_times.pop(user_key, None) if user_key else None
                if flow_start is not None:
                    flow_duration_histogram.record(
                        time.perf_counter() - flow_start,
                        {"flow": "registration_to_publish"},
                    )
