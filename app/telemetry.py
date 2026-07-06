"""OpenTelemetry SDK bootstrap for fastapi-realworld-example-app.

Call setup_telemetry() exactly once at process startup (before any
instrumented code runs).  The OTLP endpoint is read from the standard
OTEL_EXPORTER_OTLP_ENDPOINT environment variable; OTEL_SERVICE_NAME
defaults to "fastapi-realworld-example-app" if not set.
"""
from __future__ import annotations

import os
import time
from typing import Optional

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource, SERVICE_NAME
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

# ---------------------------------------------------------------------------
# Module-level instruments (created after setup_telemetry() is called)
# ---------------------------------------------------------------------------
_meter: Optional[metrics.Meter] = None

# Counters / histograms used by the application
auth_attempts_counter: Optional[metrics.Counter] = None
active_requests_gauge: Optional[metrics.UpDownCounter] = None
flow_outcomes_counter: Optional[metrics.Counter] = None
flow_duration_histogram: Optional[metrics.Histogram] = None
flow_validation_outcomes_counter: Optional[metrics.Counter] = None
flow_entry_counter: Optional[metrics.Counter] = None
flow_entry_to_terminal_histogram: Optional[metrics.Histogram] = None

_initialized = False


def setup_telemetry() -> None:
    """Build and register the global TracerProvider and MeterProvider."""
    global _initialized, _meter
    global auth_attempts_counter, active_requests_gauge
    global flow_outcomes_counter, flow_duration_histogram
    global flow_validation_outcomes_counter, flow_entry_counter
    global flow_entry_to_terminal_histogram

    if _initialized:
        return
    _initialized = True

    service_name = os.environ.get("OTEL_SERVICE_NAME", "fastapi-realworld-example-app")
    resource = Resource.create({SERVICE_NAME: service_name})

    # --- Traces ---
    otlp_span_exporter = OTLPSpanExporter()
    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(BatchSpanProcessor(otlp_span_exporter))
    trace.set_tracer_provider(tracer_provider)

    # --- Metrics ---
    otlp_metric_exporter = OTLPMetricExporter()
    metric_reader = PeriodicExportingMetricReader(otlp_metric_exporter)
    meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
    metrics.set_meter_provider(meter_provider)

    _meter = metrics.get_meter(__name__)

    # -- HTTP saturation: active in-flight requests (UpDownCounter) --
    active_requests_gauge = _meter.create_up_down_counter(
        name="http.server.active_requests",
        unit="{request}",
        description="Number of HTTP requests currently being processed.",
    )

    # -- Auth attempt outcome counter --
    auth_attempts_counter = _meter.create_counter(
        name="auth.attempts",
        unit="{attempt}",
        description="Total authentication/authorization attempts, tagged by outcome and reason.",
    )

    # -- E2E flow outcome counter --
    flow_outcomes_counter = _meter.create_counter(
        name="flow.outcomes",
        unit="{flow}",
        description="Terminal outcomes for the primary registration-to-publish flow.",
    )

    # -- E2E flow duration histogram (P95 latency SLI) --
    flow_duration_histogram = _meter.create_histogram(
        name="flow.duration",
        unit="s",
        description="End-to-end duration of the primary business flow in seconds.",
    )

    # -- Flow validation outcome counter --
    flow_validation_outcomes_counter = _meter.create_counter(
        name="flow.validation.outcomes",
        unit="{check}",
        description="Per-step validation outcomes for the primary flow.",
    )

    # -- Flow entry counter (throughput SLI) --
    flow_entry_counter = _meter.create_counter(
        name="flow.entries",
        unit="{flow}",
        description="Number of times the primary flow entry point has been invoked.",
    )

    # -- Entry-to-terminal duration histogram (freshness SLI) --
    flow_entry_to_terminal_histogram = _meter.create_histogram(
        name="flow.entry_to_terminal.duration",
        unit="s",
        description="Wall-clock time from flow entry to terminal state transition.",
    )


def get_tracer(name: str = __name__) -> trace.Tracer:
    """Return a tracer from the globally registered provider."""
    return trace.get_tracer(name)


def get_meter(name: str = __name__) -> metrics.Meter:
    """Return a meter from the globally registered provider."""
    return metrics.get_meter(name)
