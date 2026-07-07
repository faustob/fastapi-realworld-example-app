"""OpenTelemetry SDK bootstrap.

Call setup_telemetry() exactly once at process startup (before any
instrumented code runs).  The OTLP endpoint is read from the standard
environment variable OTEL_EXPORTER_OTLP_ENDPOINT; OTEL_SERVICE_NAME
sets the service name (defaults to 'fastapi-realworld-example-app').
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
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

_SERVICE_NAME = os.environ.get("OTEL_SERVICE_NAME", "fastapi-realworld-example-app")

# ---------------------------------------------------------------------------
# Instruments (module-level singletons — defined once, recorded at call sites)
# ---------------------------------------------------------------------------

# Lazy references populated by setup_telemetry()
_meter: Optional[metrics.Meter] = None

# http.server.request.duration is emitted by FastAPIInstrumentor automatically;
# we expose the meter so other modules can create additional instruments.

# Active-request up-down counter (saturation SLI)
active_requests_counter: Optional[metrics.UpDownCounter] = None

# Auth attempt outcome counter (auth-failure-rate SLI)
auth_attempts_counter: Optional[metrics.Counter] = None

# Flow outcome counter (e2e-flow-success / throughput SLIs)
flow_outcomes_counter: Optional[metrics.Counter] = None

# Flow validation outcome counter (validation-failure-rate SLI)
flow_validation_outcomes_counter: Optional[metrics.Counter] = None

# Flow entry-to-terminal duration histogram (freshness SLI)
flow_entry_to_terminal_duration: Optional[metrics.Histogram] = None


def setup_telemetry() -> None:
    """Build and register the global TracerProvider and MeterProvider."""
    global _meter
    global active_requests_counter
    global auth_attempts_counter
    global flow_outcomes_counter
    global flow_validation_outcomes_counter
    global flow_entry_to_terminal_duration

    resource = Resource.create({"service.name": _SERVICE_NAME})

    # --- Tracing ---
    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter())
    )
    trace.set_tracer_provider(tracer_provider)

    # --- Metrics ---
    metric_reader = PeriodicExportingMetricReader(OTLPMetricExporter())
    meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
    metrics.set_meter_provider(meter_provider)

    _meter = metrics.get_meter(__name__)

    # Saturation SLI — in-flight requests (up-down because it decreases)
    active_requests_counter = _meter.create_up_down_counter(
        name="http.server.active_requests",
        unit="{request}",
        description="Number of HTTP requests currently being processed.",
    )

    # Auth-failure-rate SLI
    auth_attempts_counter = _meter.create_counter(
        name="auth.attempts",
        unit="{attempt}",
        description="Total authentication/authorization attempts, tagged by outcome and reason.",
    )

    # E2E flow SLIs
    flow_outcomes_counter = _meter.create_counter(
        name="flow.outcomes",
        unit="{flow}",
        description="Terminal outcomes of the registration-to-publish business flow.",
    )

    flow_validation_outcomes_counter = _meter.create_counter(
        name="flow.validation.outcomes",
        unit="{check}",
        description="Per-step validation outcomes within the primary business flow.",
    )

    flow_entry_to_terminal_duration = _meter.create_histogram(
        name="flow.entry_to_terminal.duration",
        unit="s",
        description="Wall-clock seconds from flow entry to terminal state transition.",
    )


def get_tracer(name: str = __name__) -> trace.Tracer:
    """Return a tracer from the globally registered provider."""
    return trace.get_tracer(name)


def get_meter(name: str = __name__) -> metrics.Meter:
    """Return a meter from the globally registered provider."""
    return metrics.get_meter(name)


# ---------------------------------------------------------------------------
# Convenience helpers used by route handlers / middleware
# ---------------------------------------------------------------------------

def record_auth_attempt(outcome: str, reason: str = "") -> None:
    """Increment the auth-attempts counter.

    outcome: 'success' | 'denied'
    reason:  'wrong_password' | 'expired' | 'invalid_signature' | '' etc.
    """
    if auth_attempts_counter is None:
        return
    attrs = {"outcome": outcome}
    if reason:
        attrs["reason"] = reason
    auth_attempts_counter.add(1, attrs)


def record_flow_outcome(flow: str, outcome: str) -> None:
    """Increment the flow-outcomes counter.

    flow:    e.g. 'registration_to_publish'
    outcome: 'success' | 'failure'
    """
    if flow_outcomes_counter is None:
        return
    flow_outcomes_counter.add(1, {"flow": flow, "outcome": outcome})


def record_flow_validation_outcome(flow: str, step: str, outcome: str) -> None:
    """Increment the flow-validation-outcomes counter.

    outcome: 'passed' | 'failed'
    """
    if flow_validation_outcomes_counter is None:
        return
    flow_validation_outcomes_counter.add(1, {"flow": flow, "step": step, "outcome": outcome})


def record_flow_duration(flow: str, terminal_state: str, elapsed_seconds: float) -> None:
    """Record entry-to-terminal duration for the freshness SLI."""
    if flow_entry_to_terminal_duration is None:
        return
    flow_entry_to_terminal_duration.record(
        elapsed_seconds,
        {"flow": flow, "terminal_state": terminal_state},
    )
