"""OpenTelemetry SDK bootstrap and shared instruments for the primary business flow.

Importing this module has the (idempotent) side effect of building and
registering the global TracerProvider/MeterProvider, exporting via OTLP gRPC
using the standard OTEL_EXPORTER_OTLP_ENDPOINT / OTEL_SERVICE_NAME env vars
(never hardcoded). It also exposes the tracer/meter and the instruments used
to measure the "registration -> first article published" primary business
flow (flow.entries, flow.outcomes, flow.duration, flow.validation.outcomes).

This app runs as a single-process uvicorn server (see Dockerfile) with no
pre-fork model, so module-import-time registration is safe here.

`app.main` imports this module before building the FastAPI app, which is
what makes the registration actually run at process startup. Shutdown is
double-guarded: `app.core.events.create_stop_app_handler` calls
shutdown_telemetry() on the FastAPI shutdown event, and this module also
registers shutdown_telemetry() with atexit as a fallback for hosting setups
that bypass the ASGI shutdown event (e.g. hard process termination).
"""
import atexit
import os

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (
    OTLPMetricExporter,
)
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

_SERVICE_NAME = os.environ.get("OTEL_SERVICE_NAME", "fastapi-realworld-example-app")
_resource = Resource.create({"service.name": _SERVICE_NAME})

# set_tracer_provider()/set_meter_provider() log-and-keep-existing on
# re-registration rather than raising, so this is safe even if something else
# (e.g. a language agent) already registered a global provider.
_tracer_provider = TracerProvider(resource=_resource)
_tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
trace.set_tracer_provider(_tracer_provider)

_metric_reader = PeriodicExportingMetricReader(OTLPMetricExporter())
_meter_provider = MeterProvider(resource=_resource, metric_readers=[_metric_reader])
metrics.set_meter_provider(_meter_provider)

tracer = trace.get_tracer(__name__)
meter = metrics.get_meter(__name__)

# --- Primary flow (registration -> first article published) instruments ---

flow_entry_counter = meter.create_counter(
    name="flow.entries",
    unit="1",
    description=(
        "Number of times the primary flow's entry point (user registration) "
        "was invoked, independent of eventual outcome"
    ),
)

flow_outcome_counter = meter.create_counter(
    name="flow.outcomes",
    unit="1",
    description=(
        "Terminal outcomes (success/failure) of steps within the primary "
        "registration-to-first-article-published business flow"
    ),
)

flow_duration_histogram = meter.create_histogram(
    name="flow.duration",
    unit="s",
    description="Duration of the primary business flow's terminal step, in seconds",
)

flow_validation_counter = meter.create_counter(
    name="flow.validation.outcomes",
    unit="1",
    description="Pass/fail outcomes of individual validation steps within the primary flow",
)


def current_flow_id() -> str:
    """Return the current trace id (hex) to correlate flow steps/spans."""
    span_context = trace.get_current_span().get_span_context()
    if span_context is None or not span_context.is_valid:
        return "0"
    return format(span_context.trace_id, "032x")


def instrument_fastapi_app(app) -> None:  # noqa: ANN001
    """Attach the standard FastAPI/ASGI OTel auto-instrumentation.

    Emits the semantic-convention http.server.request.duration histogram
    (method/route/status attributes) for every inbound request.
    """
    FastAPIInstrumentor.instrument_app(app)


def shutdown_telemetry() -> None:
    """Flush and shut down the global TracerProvider/MeterProvider.

    Called on process shutdown (see app.core.events.create_stop_app_handler,
    wired to the FastAPI "shutdown" event in app.main.get_application) so
    spans/metrics buffered in the BatchSpanProcessor/PeriodicExportingMetricReader
    for the final export window are not lost. Safe to call more than once.
    """
    _tracer_provider.shutdown()
    _meter_provider.shutdown()


# Fallback: guarantee a flush attempt even if the ASGI shutdown event never
# fires (e.g. the process is stopped in a way that bypasses it).
atexit.register(shutdown_telemetry)
