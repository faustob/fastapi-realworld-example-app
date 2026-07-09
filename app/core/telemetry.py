import os
import time

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
    OTLPMetricExporter,
)
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from starlette.requests import Request
from starlette.responses import Response

_INITIALIZED = False

_SERVICE_NAME = os.environ.get("OTEL_SERVICE_NAME", "fastapi-realworld-example-app")

# Module-level tracer/meter — these are safe to create before the real
# providers are registered; the API returns proxy objects that rebind once
# set_tracer_provider/set_meter_provider is called in setup_telemetry().
tracer = trace.get_tracer(__name__)
meter = metrics.get_meter(__name__)

http_request_duration = meter.create_histogram(
    name="http.server.request.duration",
    unit="s",
    description="Duration of inbound HTTP requests",
)

http_active_requests = meter.create_up_down_counter(
    name="http.server.active_requests",
    unit="{request}",
    description="Number of in-flight HTTP requests",
)

http_request_outcome_total = meter.create_counter(
    name="http.server.request.outcome.total",
    unit="{request}",
    description="Count of HTTP requests labeled by route and outcome class",
)

auth_attempts_total = meter.create_counter(
    name="auth.attempts.total",
    unit="{attempt}",
    description="Count of authentication attempts labeled by outcome and reason",
)

flow_outcomes_total = meter.create_counter(
    name="flow.outcomes.total",
    unit="{flow}",
    description="Count of primary business flow terminal outcomes",
)

flow_duration = meter.create_histogram(
    name="flow.duration",
    unit="s",
    description="End-to-end duration of the primary business flow",
)

flow_validation_outcomes_total = meter.create_counter(
    name="flow.validation.outcomes.total",
    unit="{check}",
    description="Count of per-step flow validation outcomes",
)

flow_entry_to_terminal_duration = meter.create_histogram(
    name="flow.entry_to_terminal.duration",
    unit="s",
    description="Wall-clock time between a flow's entry event and its terminal state",
)


def setup_telemetry() -> None:
    """Build and register the global TracerProvider/MeterProvider exactly once.

    Registration is defensive: if a provider (e.g. from an auto-attached
    agent) is already installed, the OTel API logs and keeps the existing
    one rather than raising, so this is safe to call unconditionally.
    """
    global _INITIALIZED
    if _INITIALIZED:
        return
    _INITIALIZED = True

    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")

    resource = Resource.create({"service.name": _SERVICE_NAME})

    tracer_provider = TracerProvider(resource=resource)
    span_exporter = OTLPSpanExporter(endpoint=endpoint) if endpoint else OTLPSpanExporter()
    tracer_provider.add_span_processor(BatchSpanProcessor(span_exporter))
    trace.set_tracer_provider(tracer_provider)

    metric_exporter = (
        OTLPMetricExporter(endpoint=endpoint) if endpoint else OTLPMetricExporter()
    )
    metric_reader = PeriodicExportingMetricReader(metric_exporter)
    meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
    metrics.set_meter_provider(meter_provider)


def _status_outcome(status_code: int) -> str:
    if status_code >= 500:
        return "error"
    if status_code >= 400:
        return "client_error"
    return "success"


async def telemetry_middleware(request: Request, call_next):
    """Records http.server.request.duration, active-request gauge, and
    per-route outcome counters. Complements FastAPIInstrumentor (which emits
    the semconv histogram with method/route/status attributes) by adding the
    active-requests up-down counter and low-cardinality outcome counter used
    for availability/error-rate SLIs.
    """
    method = request.method
    route = request.scope.get("route")
    route_template = getattr(route, "path", request.url.path)

    http_active_requests.add(1, {"http.request.method": method})
    start = time.monotonic()
    status_code = 500
    error_type = None
    try:
        response: Response = await call_next(request)
        status_code = response.status_code
        return response
    except Exception as exc:  # noqa: BLE001 - re-raised unchanged, telemetry only
        error_type = type(exc).__name__
        span = trace.get_current_span()
        span.set_attribute("error.type", error_type)
        raise
    finally:
        duration = time.monotonic() - start
        attributes = {
            "http.request.method": method,
            "http.route": route_template,
            "http.response.status_code": status_code,
            "url.scheme": request.url.scheme,
        }
        if error_type:
            attributes["error.type"] = error_type
        http_request_duration.record(duration, attributes)
        http_request_outcome_total.add(
            1,
            {
                "http.route": route_template,
                "outcome": _status_outcome(status_code),
            },
        )
        http_active_requests.add(-1, {"http.request.method": method})
