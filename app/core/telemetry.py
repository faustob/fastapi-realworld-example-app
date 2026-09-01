"""OpenTelemetry SDK bootstrap and shared instruments for the primary business flow.

This module builds and registers the global TracerProvider/MeterProvider, with OTLP
exporters pointed at OTEL_EXPORTER_OTLP_ENDPOINT (env-driven, never hardcoded). It is
imported for its side effect from app/main.py before the FastAPI application (and
therefore any instrumented request path) is built, so the global providers are
registered before requests are served.

`opentelemetry.trace.set_tracer_provider` / `opentelemetry.metrics.set_meter_provider`
do not raise if a provider is already registered (e.g. by an attached language
agent) -- they log and keep the existing one -- so no extra guarding is required.
Instruments obtained via `metrics.get_meter` / `trace.get_tracer` are proxy objects
that transparently rebind to whichever provider ends up registered.

Deployment shape: this app is deployed as a single uvicorn process (see Dockerfile /
process entrypoint) -- there is no pre-fork server (gunicorn/uWSGI) configured in this
repo, so module-import-time initialization runs exactly once and is safe as-is. We do
NOT unconditionally install an os.register_at_fork hook, since rebuilding providers on
every fork is only meaningful (and correct) under an actual pre-fork model, and doing
it unconditionally would add fork-time work with nothing to justify it here. If this
service is ever deployed behind a pre-fork server, set
OTEL_PYTHON_RESET_PROVIDERS_ON_FORK=true in that deployment's environment so providers
are rebuilt fresh in each forked child (never by shutting down the parent's inherited
BatchSpanProcessor first -- that join would hang the child, per upstream OTel guidance).
"""
import atexit
import os

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

_SERVICE_NAME = os.environ.get("OTEL_SERVICE_NAME", "fastapi-realworld-example-app")
_resource = Resource.create({"service.name": _SERVICE_NAME})

_tracer_provider = None
_meter_provider = None


def _build_and_register_providers() -> None:
    global _tracer_provider, _meter_provider  # noqa: WPS420, WPS global-statement

    tracer_provider = TracerProvider(resource=_resource)
    tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(tracer_provider)

    metric_reader = PeriodicExportingMetricReader(OTLPMetricExporter())
    meter_provider = MeterProvider(resource=_resource, metric_readers=[metric_reader])
    metrics.set_meter_provider(meter_provider)

    _tracer_provider = tracer_provider
    _meter_provider = meter_provider


def _shutdown_providers() -> None:
    if _tracer_provider is not None:
        _tracer_provider.shutdown()
    if _meter_provider is not None:
        _meter_provider.shutdown()


_build_and_register_providers()
atexit.register(_shutdown_providers)

# Only rebuild providers post-fork when a pre-fork deployment explicitly opts in --
# unconditionally installing this hook would add fork-time work with no configured
# pre-fork model in this repo (single-process uvicorn).
_RESET_PROVIDERS_ON_FORK = os.environ.get(
    "OTEL_PYTHON_RESET_PROVIDERS_ON_FORK",
    "false",
).lower() == "true"

if _RESET_PROVIDERS_ON_FORK and hasattr(os, "register_at_fork"):
    # Pre-fork server (gunicorn/uWSGI) opt-in: rebuild fresh providers in the child
    # instead of reusing the parent's inherited export thread/lock.
    os.register_at_fork(after_in_child=_build_and_register_providers)

tracer = trace.get_tracer("app.flow")
meter = metrics.get_meter("app.flow")

# --- Primary business flow (article publishing) instruments ----------------------

flow_entry_counter = meter.create_counter(
    name="flow.entries",
    unit="1",
    description="Number of times the primary business flow's entry point (article publish) was invoked, independent of outcome.",
)

flow_outcome_counter = meter.create_counter(
    name="flow.outcomes",
    unit="1",
    description="Terminal outcome of the primary business flow, labeled by outcome (success/failure).",
)

flow_duration_histogram = meter.create_histogram(
    name="flow.duration",
    unit="s",
    description="End-to-end duration of the primary business flow, in seconds.",
)

validation_outcome_counter = meter.create_counter(
    name="flow.validation.outcomes",
    unit="1",
    description="Outcome of each validation step within the primary business flow, labeled by outcome (passed/failed).",
)
