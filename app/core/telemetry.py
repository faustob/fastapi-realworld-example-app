"""OpenTelemetry bootstrap and shared instruments for the application.

This module builds and registers the global TracerProvider and
MeterProvider exactly once at process startup (invoked from app.main
before the FastAPI application is constructed). It also exposes shared
instruments (counters/histograms/gauges) used across the codebase for
SLI recording, plus a saturation middleware and an exception handler
helper.

No agent is assumed to be attached, but registration is defensive:
if a provider is already set (e.g. by an externally attached agent),
we log and continue using the existing global provider instead of
crashing the app.
"""
import logging
import os
import time
from typing import Callable

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger(__name__)

_TELEMETRY_CONFIGURED = False

SERVICE_NAME_VALUE = os.environ.get("OTEL_SERVICE_NAME", "fastapi-realworld-example-app")


def configure_telemetry() -> None:
    """Build and register the global tracer/meter providers exactly once.

    Defensive against an already-registered global provider (e.g. from
    an externally attached agent) -- in that case we simply keep using
    the existing global provider.
    """
    global _TELEMETRY_CONFIGURED
    if _TELEMETRY_CONFIGURED:
        return

    resource = Resource.create({SERVICE_NAME: SERVICE_NAME_VALUE})

    otlp_endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")

    try:
        tracer_provider = TracerProvider(resource=resource)
        span_exporter_kwargs = {}
        if otlp_endpoint:
            span_exporter_kwargs["endpoint"] = f"{otlp_endpoint}/v1/traces"
        tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(**span_exporter_kwargs)))
        trace.set_tracer_provider(tracer_provider)
    except Exception:  # pragma: no cover - defensive against re-registration
        logger.warning("TracerProvider already registered; using existing global provider", exc_info=True)

    try:
        metric_exporter_kwargs = {}
        if otlp_endpoint:
            metric_exporter_kwargs["endpoint"] = f"{otlp_endpoint}/v1/metrics"
        metric_reader = PeriodicExportingMetricReader(OTLPMetricExporter(**metric_exporter_kwargs))
        meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
        metrics.set_meter_provider(meter_provider)
    except Exception:  # pragma: no cover - defensive against re-registration
        logger.warning("MeterProvider already registered; using existing global provider", exc_info=True)

    _TELEMETRY_CONFIGURED = True


tracer = trace.get_tracer(__name__)
meter = metrics.get_meter(__name__)

# --- HTTP availability / error-rate ---------------------------------------
http_request_outcome_counter = meter.create_counter(
    name="http.server.request.outcome",
    description="Count of HTTP requests labeled by route and outcome class",
    unit="1",
)

# --- HTTP saturation --------------------------------------------------------
http_active_requests = meter.create_up_down_counter(
    name="http.server.active_requests",
    description="Number of in-flight HTTP requests",
    unit="{request}",
)

_worker_pool_size_value = int(os.environ.get("WEB_CONCURRENCY", "1"))


def _observe_worker_pool_size(options):
    yield metrics.Observation(_worker_pool_size_value, {})


meter.create_observable_gauge(
    name="http.server.worker_pool.size",
    description="Configured size of the uvicorn worker pool",
    unit="{worker}",
    callbacks=[_observe_worker_pool_size],
)

# --- Auth failure rate -------------------------------------------------------
auth_attempts_counter = meter.create_counter(
    name="auth.attempts",
    description="Count of authentication/authorization decisions tagged by outcome and reason",
    unit="1",
)

# --- Per-tenant / per-key request throughput --------------------------------
http_request_rate_counter = meter.create_counter(
    name="http.server.request.count",
    description="Count of HTTP requests broken out by tenant/API key",
    unit="1",
)

# --- Business flow (registration-to-publish) --------------------------------
flow_outcome_counter = meter.create_counter(
    name="flow.outcomes",
    description="Terminal outcome count for the primary registration-to-publish business flow",
    unit="1",
)

flow_duration_histogram = meter.create_histogram(
    name="flow.duration",
    description="End-to-end duration of the primary business flow",
    unit="s",
)

flow_entry_counter = meter.create_counter(
    name="flow.entry.count",
    description="Count of entries into the primary business flow, independent of eventual outcome",
    unit="1",
)

flow_validation_outcome_counter = meter.create_counter(
    name="flow.validation.outcomes",
    description="Outcome count for each validation step of the primary business flow",
    unit="1",
)

flow_entry_to_terminal_histogram = meter.create_histogram(
    name="flow.entry_to_terminal.duration",
    description="Wall-clock time between flow entry and terminal state transition",
    unit="s",
)

# P99 slow-request budget (seconds) used to emit a span event for triage.
_SLOW_REQUEST_BUDGET_SECONDS = float(os.environ.get("HTTP_P99_BUDGET_SECONDS", "0.75"))


async def saturation_middleware(request: Request, call_next: Callable) -> Response:
    """Track in-flight requests and record per-route outcome/latency metrics.

    This complements FastAPIInstrumentor's own http.server.request.duration
    histogram with the outcome counter, tenant-tagged rate counter, active
    request gauge and slow-request span event required by the SLIs.
    """
    route_template = request.scope.get("route")
    route = route_template.path if route_template is not None else request.url.path
    method = request.method
    tenant = request.headers.get("x-api-key") or request.headers.get("authorization", "anonymous")
    tenant_bucket = "authenticated" if tenant != "anonymous" else "anonymous"

    http_active_requests.add(1, {"http.route": route})
    start = time.monotonic()
    status_code = 500
    error_type = None
    try:
        response = await call_next(request)
        status_code = response.status_code
        return response
    except Exception as exc:
        error_type = type(exc).__name__
        raise
    finally:
        elapsed = time.monotonic() - start
        http_active_requests.add(-1, {"http.route": route})

        outcome = "success" if status_code < 500 else "failure"
        attrs = {
            "http.route": route,
            "http.request.method": method,
            "http.response.status_code": status_code,
            "outcome": outcome,
        }
        if error_type:
            attrs["error.type"] = error_type
        http_request_outcome_counter.add(1, attrs)
        http_request_rate_counter.add(1, {"http.route": route, "tenant": tenant_bucket})

        if elapsed > _SLOW_REQUEST_BUDGET_SECONDS:
            span = trace.get_current_span()
            span.add_event(
                "slow_request.p99_budget_exceeded",
                {
                    "http.route": route,
                    "http.request.method": method,
                    "duration_seconds": elapsed,
                    "budget_seconds": _SLOW_REQUEST_BUDGET_SECONDS,
                },
            )


def instrument_fastapi_app(app) -> None:
    """Install the official FastAPI OTel instrumentation on the app."""
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    FastAPIInstrumentor.instrument_app(app)
