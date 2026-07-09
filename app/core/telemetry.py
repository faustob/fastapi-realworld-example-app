"""OpenTelemetry SDK bootstrap and shared instrumentation helpers.

This module:
1. Builds and globally registers TracerProvider + MeterProvider (OTLP/HTTP).
2. Instruments the FastAPI application via FastAPIInstrumentor.
3. Exposes shared meters/counters/histograms used by route handlers.

Call setup_telemetry(app) ONCE at application startup, before any request is served.
"""
import os
import time
from typing import Optional

from fastapi import FastAPI
from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource, SERVICE_NAME
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

# ---------------------------------------------------------------------------
# Module-level instrument singletons (populated by setup_telemetry)
# ---------------------------------------------------------------------------
_meter: Optional[metrics.Meter] = None

# HTTP saturation: active (in-flight) requests — UpDownCounter because it goes up AND down
_active_requests_counter: Optional[metrics.UpDownCounter] = None

# Auth attempt outcome counter
_auth_attempts_counter: Optional[metrics.Counter] = None

# Flow outcome counter (registration-to-publish)
_flow_outcomes_counter: Optional[metrics.Counter] = None

# Flow entry counter (throughput)
_flow_entry_counter: Optional[metrics.Counter] = None

# Flow validation outcome counter
_flow_validation_counter: Optional[metrics.Counter] = None

# Flow entry-to-terminal duration histogram (freshness)
_flow_entry_to_terminal_histogram: Optional[metrics.Histogram] = None


def setup_telemetry(app: FastAPI) -> None:
    """Register the OTel SDK globally and instrument the FastAPI app.

    Safe to call multiple times — if a global provider is already registered
    (e.g. by a Java/Python agent or a test fixture) the existing provider is
    kept and only the FastAPI instrumentation is applied.
    """
    global _meter, _active_requests_counter, _auth_attempts_counter
    global _flow_outcomes_counter, _flow_entry_counter, _flow_validation_counter
    global _flow_entry_to_terminal_histogram

    service_name = os.environ.get("OTEL_SERVICE_NAME", "fastapi-realworld-example-app")
    otlp_endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")

    resource = Resource.create({SERVICE_NAME: service_name})

    # ------------------------------------------------------------------
    # TracerProvider — guard against double-registration (agent present)
    # ------------------------------------------------------------------
    try:
        tracer_provider = TracerProvider(resource=resource)
        span_exporter = OTLPSpanExporter(
            endpoint=otlp_endpoint.rstrip("/") + "/v1/traces",
        )
        tracer_provider.add_span_processor(BatchSpanProcessor(span_exporter))
        trace.set_tracer_provider(tracer_provider)
    except Exception:  # noqa: BLE001 — already set by agent; continue with existing
        pass

    # ------------------------------------------------------------------
    # MeterProvider — guard against double-registration
    # ------------------------------------------------------------------
    try:
        metric_exporter = OTLPMetricExporter(
            endpoint=otlp_endpoint.rstrip("/") + "/v1/metrics",
        )
        metric_reader = PeriodicExportingMetricReader(metric_exporter)
        meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
        metrics.set_meter_provider(meter_provider)
    except Exception:  # noqa: BLE001 — already set by agent; continue with existing
        pass

    # ------------------------------------------------------------------
    # Shared meter and instruments
    # ------------------------------------------------------------------
    _meter = metrics.get_meter(__name__)

    # HTTP saturation — in-flight requests (UpDownCounter: can go negative)
    _active_requests_counter = _meter.create_up_down_counter(
        name="http.server.active_requests",
        unit="{request}",
        description="Number of HTTP requests currently being processed.",
    )

    # Auth attempt outcome counter
    _auth_attempts_counter = _meter.create_counter(
        name="auth.attempts",
        unit="{attempt}",
        description="Count of authentication/authorization decisions, tagged by outcome and reason.",
    )

    # Flow outcome counter
    _flow_outcomes_counter = _meter.create_counter(
        name="flow.outcomes",
        unit="{flow}",
        description="Terminal outcomes of the registration-to-publish business flow.",
    )

    # Flow entry counter (throughput)
    _flow_entry_counter = _meter.create_counter(
        name="flow.entries",
        unit="{flow}",
        description="Number of times the primary business flow entry point was invoked.",
    )

    # Flow validation outcome counter
    _flow_validation_counter = _meter.create_counter(
        name="flow.validation.outcomes",
        unit="{check}",
        description="Outcomes of per-step flow validation checks.",
    )

    # Flow entry-to-terminal duration histogram (freshness SLI)
    _flow_entry_to_terminal_histogram = _meter.create_histogram(
        name="flow.entry_to_terminal.duration",
        unit="s",
        description="Wall-clock seconds between flow entry and terminal state transition.",
    )

    # ------------------------------------------------------------------
    # FastAPI auto-instrumentation (emits http.server.request.duration)
    # ------------------------------------------------------------------
    FastAPIInstrumentor.instrument_app(
        app,
        tracer_provider=trace.get_tracer_provider(),
        meter_provider=metrics.get_meter_provider(),
    )


# ---------------------------------------------------------------------------
# Public helpers — call these from route handlers / middleware
# ---------------------------------------------------------------------------

def record_active_request_start(method: str) -> None:
    """Increment the in-flight request counter (call at handler entry)."""
    if _active_requests_counter is not None:
        _active_requests_counter.add(1, {"http.request.method": method})


def record_active_request_end(method: str) -> None:
    """Decrement the in-flight request counter (call at handler exit)."""
    if _active_requests_counter is not None:
        _active_requests_counter.add(-1, {"http.request.method": method})


def record_auth_attempt(outcome: str, reason: str = "") -> None:
    """Record an authentication/authorization decision.

    Args:
        outcome: "success" or "denied".
        reason:  Low-cardinality denial reason, e.g. "expired", "invalid_signature",
                 "wrong_password", "missing_token".  Empty string for successes.
    """
    if _auth_attempts_counter is not None:
        attrs = {"outcome": outcome}
        if reason:
            attrs["reason"] = reason
        _auth_attempts_counter.add(1, attrs)


def record_flow_entry(flow: str = "registration_to_publish") -> None:
    """Increment the flow-entry counter (throughput SLI)."""
    if _flow_entry_counter is not None:
        _flow_entry_counter.add(1, {"flow": flow})


def record_flow_outcome(outcome: str, flow: str = "registration_to_publish") -> None:
    """Record a terminal flow outcome.

    Args:
        outcome: "success" or "failure".
        flow:    Flow identifier.
    """
    if _flow_outcomes_counter is not None:
        _flow_outcomes_counter.add(1, {"flow": flow, "outcome": outcome})


def record_flow_validation(step: str, outcome: str, flow: str = "registration_to_publish") -> None:
    """Record a per-step validation outcome.

    Args:
        step:    Low-cardinality step name, e.g. "email_format", "username_unique".
        outcome: "passed" or "failed".
        flow:    Flow identifier.
    """
    if _flow_validation_counter is not None:
        _flow_validation_counter.add(1, {"flow": flow, "step": step, "outcome": outcome})


def record_flow_duration(elapsed_seconds: float, terminal_state: str, flow: str = "registration_to_publish") -> None:
    """Record the entry-to-terminal wall-clock duration (freshness SLI).

    Args:
        elapsed_seconds: Seconds since flow entry.
        terminal_state:  Low-cardinality terminal state, e.g. "article_published", "failed".
        flow:            Flow identifier.
    """
    if _flow_entry_to_terminal_histogram is not None:
        _flow_entry_to_terminal_histogram.record(
            elapsed_seconds,
            {"flow": flow, "terminal_state": terminal_state},
        )
