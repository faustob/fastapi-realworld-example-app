"""OpenTelemetry SDK bootstrap and shared instruments.

Call ``setup_telemetry()`` exactly once at process startup (done in
``app/main.py`` inside ``get_application()``) before any instrumented code
runs.  All other modules import the pre-built instruments directly.
"""
import os
from typing import Optional

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

_SERVICE_NAME = os.getenv("OTEL_SERVICE_NAME", "fastapi-realworld-example-app")
_OTLP_ENDPOINT = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")

_tracer_provider: Optional[TracerProvider] = None


def setup_telemetry() -> None:
    """Build and register the global TracerProvider and MeterProvider.

    Safe to call multiple times — subsequent calls are no-ops.
    """
    global _tracer_provider  # noqa: WPS420
    if _tracer_provider is not None:
        return

    resource = Resource.create({"service.name": _SERVICE_NAME})

    # --- Traces ---
    span_exporter_kwargs = {}
    if _OTLP_ENDPOINT:
        span_exporter_kwargs["endpoint"] = _OTLP_ENDPOINT
    span_exporter = OTLPSpanExporter(**span_exporter_kwargs)
    _tracer_provider = TracerProvider(resource=resource)
    _tracer_provider.add_span_processor(BatchSpanProcessor(span_exporter))
    trace.set_tracer_provider(_tracer_provider)

    # --- Metrics ---
    metric_exporter_kwargs = {}
    if _OTLP_ENDPOINT:
        metric_exporter_kwargs["endpoint"] = _OTLP_ENDPOINT
    metric_exporter = OTLPMetricExporter(**metric_exporter_kwargs)
    reader = PeriodicExportingMetricReader(metric_exporter)
    meter_provider = MeterProvider(resource=resource, metric_readers=[reader])
    metrics.set_meter_provider(meter_provider)

    # Build instruments now that the real MeterProvider is registered.
    _init_instruments()


def get_tracer() -> trace.Tracer:
    """Return the global tracer for this service."""
    return trace.get_tracer(__name__)


# ---------------------------------------------------------------------------
# Shared meter and instruments
# ---------------------------------------------------------------------------
# Instruments are initialised lazily by _init_instruments(), called from
# setup_telemetry() AFTER the real MeterProvider has been registered globally.
# Module-level names are set to None until then; callers must only use them
# after setup_telemetry() has been called (guaranteed by get_application()).

active_requests = None
auth_attempts = None
flow_entry_counter = None
flow_outcomes = None
flow_duration = None
flow_validation_outcomes = None


def _init_instruments() -> None:
    """Create all shared instruments against the now-registered MeterProvider."""
    global active_requests, auth_attempts, flow_entry_counter  # noqa: WPS420
    global flow_outcomes, flow_duration, flow_validation_outcomes  # noqa: WPS420

    _meter = metrics.get_meter(__name__)

    # --- HTTP saturation: active in-flight requests (UpDownCounter — can go up and down) ---
    active_requests = _meter.create_up_down_counter(
        name="http.server.active_requests",
        description="Number of HTTP requests currently being processed.",
        unit="{request}",
    )

    # --- Authentication SLI: every auth decision tagged with outcome + reason ---
    auth_attempts = _meter.create_counter(
        name="auth.attempts",
        description="Count of authentication/authorisation decisions.",
        unit="{attempt}",
    )

    # --- E2E flow: entry counter (throughput) ---
    flow_entry_counter = _meter.create_counter(
        name="flow.entries",
        description="Number of times the primary business flow entry point is invoked.",
        unit="{invocation}",
    )

    # --- E2E flow: terminal outcome counter (success-rate SLI) ---
    flow_outcomes = _meter.create_counter(
        name="flow.outcomes",
        description="Terminal outcomes of the primary business flow.",
        unit="{outcome}",
    )

    # --- E2E flow: entry-to-terminal duration histogram (freshness / latency SLI) ---
    flow_duration = _meter.create_histogram(
        name="flow.entry_to_terminal.duration",
        description="Wall-clock time from flow entry to terminal state transition.",
        unit="s",
    )

    # --- E2E flow: per-step validation outcome counter ---
    flow_validation_outcomes = _meter.create_counter(
        name="flow.validation.outcomes",
        description="Outcome of each validation step within the primary business flow.",
        unit="{outcome}",
    )
