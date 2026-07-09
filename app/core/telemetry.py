import logging
import os

from starlette.requests import Request
from starlette.responses import JSONResponse

from opentelemetry import metrics, trace
from opentelemetry.trace import Status, StatusCode
from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
    OTLPMetricExporter,
)
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

logger = logging.getLogger(__name__)

_initialized = False


def setup_telemetry(service_name: str) -> None:
    """Build and register the global OTel TracerProvider/MeterProvider exactly once.

    Defensive: if a provider is already registered (e.g. by an attached agent),
    the SDK's set_* calls simply log and keep the existing provider — they never
    raise — so this is safe to call even under an agent.
    """
    global _initialized
    if _initialized:
        return
    _initialized = True

    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")

    resource = Resource.create({"service.name": service_name})

    try:
        tracer_provider = TracerProvider(resource=resource)
        span_exporter = OTLPSpanExporter(endpoint=endpoint) if endpoint else OTLPSpanExporter()
        tracer_provider.add_span_processor(BatchSpanProcessor(span_exporter))
        trace.set_tracer_provider(tracer_provider)
    except Exception:
        logger.exception("failed to initialize OTel tracer provider")

    try:
        metric_exporter = (
            OTLPMetricExporter(endpoint=endpoint) if endpoint else OTLPMetricExporter()
        )
        metric_reader = PeriodicExportingMetricReader(metric_exporter)
        meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
        metrics.set_meter_provider(meter_provider)
    except Exception:
        logger.exception("failed to initialize OTel meter provider")


_meter = metrics.get_meter(__name__)
_tracer = trace.get_tracer(__name__)

request_outcome_counter = _meter.create_counter(
    "http.server.request.outcomes",
    description="Count of HTTP requests labeled by route and outcome class",
)

auth_attempts_counter = _meter.create_counter(
    "auth.attempts",
    description="Count of authentication attempts labeled by outcome and denial reason",
)

flow_outcomes_counter = _meter.create_counter(
    "flow.outcomes",
    description="Count of end-to-end business flow terminal outcomes",
)

flow_entry_counter = _meter.create_counter(
    "flow.entry.count",
    description="Count of entries into the primary business flow",
)

flow_duration_histogram = _meter.create_histogram(
    "flow.duration",
    description="End-to-end duration of the primary business flow",
    unit="s",
)

flow_validation_outcomes_counter = _meter.create_counter(
    "flow.validation.outcomes",
    description="Count of per-step flow validation outcomes",
)

entry_to_terminal_histogram = _meter.create_histogram(
    "flow.entry_to_terminal.duration",
    description="Wall-clock time between flow entry event and terminal state transition",
    unit="s",
)

active_requests_updown = _meter.create_up_down_counter(
    "http.server.active_requests",
    description="Number of in-flight HTTP requests",
)


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Record the originating exception type on the current server span and
    increment the request outcome counter for the 5xx class, then respond 500.

    This does not change existing behavior for handled exceptions (HTTPException,
    RequestValidationError) which are handled by their own registered handlers.
    """
    span = trace.get_current_span()
    error_type = type(exc).__name__
    span.set_attribute("error.type", error_type)
    span.set_status(Status(StatusCode.ERROR, str(exc)))

    route = request.scope.get("route")
    route_template = getattr(route, "path", request.url.path)

    request_outcome_counter.add(
        1,
        {
            "http.route": route_template,
            "http.request.method": request.method,
            "outcome": "error",
            "error.type": error_type,
        },
    )

    raise exc
