"""OpenTelemetry SDK bootstrap and shared instrumentation helpers.

Call setup_telemetry() once at process startup (before any request is served).
Call instrument_app(app) after the FastAPI application object is fully built.

All other modules obtain their tracer/meter via get_tracer(__name__) /
get_meter(__name__) — they never call setup_telemetry() themselves.
"""
import os
import time
from typing import Callable

from fastapi import FastAPI, Request, Response
from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
SERVICE_NAME = os.getenv("OTEL_SERVICE_NAME", "fastapi-realworld-example-app")
OTLP_ENDPOINT = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")

# P99 budget in seconds — requests exceeding this get a span event for triage
P99_BUDGET_SECONDS = float(os.getenv("P99_BUDGET_SECONDS", "0.75"))

_setup_done = False


def setup_telemetry() -> None:
    """Build and register the global TracerProvider and MeterProvider.

    Safe to call multiple times; subsequent calls are no-ops.
    """
    global _setup_done
    if _setup_done:
        return

    resource = Resource.create(
        {
            "service.name": SERVICE_NAME,
        }
    )

    # --- Traces ---
    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=OTLP_ENDPOINT))
    )
    trace.set_tracer_provider(tracer_provider)

    # --- Metrics ---
    metric_reader = PeriodicExportingMetricReader(
        OTLPMetricExporter(endpoint=OTLP_ENDPOINT)
    )
    meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
    metrics.set_meter_provider(meter_provider)

    _setup_done = True


# ---------------------------------------------------------------------------
# Shared meter and instruments
# (defined once here; imported by middleware and route helpers)
# ---------------------------------------------------------------------------

def _get_meter():
    return metrics.get_meter(__name__)


# HTTP request outcome counter  (availability + error-rate SLIs)
# Dimensions: http.route, http.request.method, http.response.status_code, outcome
_http_requests_counter = None

# Active-requests UpDownCounter  (saturation SLI)
_active_requests_counter = None

# Flow outcome counter  (e2e flow success + throughput SLIs)
_flow_outcomes_counter = None

# Flow entry counter  (e2e flow throughput SLI)
_flow_entries_counter = None

# Flow entry-to-terminal duration histogram  (freshness SLI)
_flow_duration_histogram = None

# Auth attempt outcome counter  (auth failure-rate SLI)
_auth_attempts_counter = None

# Flow validation outcome counter  (validation failure-rate SLI)
_flow_validation_counter = None


def _ensure_instruments() -> None:
    """Lazily initialise instruments after the global MeterProvider is set."""
    global _http_requests_counter, _active_requests_counter, _flow_outcomes_counter, _flow_entries_counter, _flow_duration_histogram, _auth_attempts_counter, _flow_validation_counter
    if _http_requests_counter is not None:
        return
    meter = _get_meter()

    _http_requests_counter = meter.create_counter(
        name="http.server.requests.total",
        description="Total HTTP server requests, labelled by route, method, status code and outcome.",
        unit="{request}",
    )

    # UpDownCounter — value can go up AND down (in-flight requests)
    _active_requests_counter = meter.create_up_down_counter(
        name="http.server.active_requests",
        description="Number of HTTP requests currently being processed.",
        unit="{request}",
    )

    _flow_outcomes_counter = meter.create_counter(
        name="flow.outcomes",
        description="Terminal outcomes of the primary registration-to-publish flow.",
        unit="{flow}",
    )

    _flow_entries_counter = meter.create_counter(
        name="flow.entries",
        description="Number of times the primary flow entry point has been invoked.",
        unit="{flow}",
    )

    _flow_duration_histogram = meter.create_histogram(
        name="flow.entry_to_terminal.duration",
        description="Wall-clock time from flow entry to terminal state transition.",
        unit="s",
    )

    _auth_attempts_counter = meter.create_counter(
        name="auth.attempts",
        description="Authentication/authorisation decisions, labelled by outcome and denial reason.",
        unit="{attempt}",
    )

    _flow_validation_counter = meter.create_counter(
        name="flow.validation.outcomes",
        description="Per-step validation outcomes for the primary flow.",
        unit="{check}",
    )


# ---------------------------------------------------------------------------
# Public recording helpers — import these from route/service modules
# ---------------------------------------------------------------------------

def record_auth_attempt(outcome: str, reason: str = "") -> None:
    """Record one auth attempt.

    outcome: "success" | "denied"
    reason:  "" | "expired" | "invalid_signature" | "wrong_password" | ...
    """
    _ensure_instruments()
    attrs = {"outcome": outcome}
    if reason:
        attrs["denial.reason"] = reason
    _auth_attempts_counter.add(1, attrs)


def record_flow_entry(flow: str = "registration_to_publish") -> None:
    """Increment the flow-entry counter."""
    _ensure_instruments()
    _flow_entries_counter.add(1, {"flow": flow})


def record_flow_outcome(outcome: str, flow: str = "registration_to_publish") -> None:
    """Record a terminal flow outcome ("success" | "failure")."""
    _ensure_instruments()
    _flow_outcomes_counter.add(1, {"flow": flow, "outcome": outcome})


def record_flow_duration(elapsed_seconds: float, terminal_state: str, flow: str = "registration_to_publish") -> None:
    """Record entry-to-terminal wall-clock duration."""
    _ensure_instruments()
    _flow_duration_histogram.record(
        elapsed_seconds,
        {"flow": flow, "terminal_state": terminal_state},
    )


def record_flow_validation(step: str, outcome: str, flow: str = "registration_to_publish") -> None:
    """Record a per-step validation outcome ("passed" | "failed")."""
    _ensure_instruments()
    _flow_validation_counter.add(1, {"flow": flow, "step": step, "outcome": outcome})


# ---------------------------------------------------------------------------
# ASGI middleware — active-requests gauge + request outcome counter
# ---------------------------------------------------------------------------

class TelemetryMiddleware:
    """Lightweight ASGI middleware that:

    1. Tracks in-flight requests via an UpDownCounter (saturation SLI).
    2. Emits a request-outcome counter after each response (availability SLI).
    3. Adds a span event when a request exceeds the P99 latency budget.
    """

    def __init__(self, app) -> None:
        self.app = app
        _ensure_instruments()

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        _active_requests_counter.add(1)
        start = time.perf_counter()
        status_code = 500

        async def send_wrapper(message):
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message.get("status", 500)
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            elapsed = time.perf_counter() - start
            _active_requests_counter.add(-1)

            # Derive route from scope (set by Starlette/FastAPI router)
            route = "unknown"
            if scope.get("route"):
                route = getattr(scope["route"], "path", "unknown")
            elif scope.get("path"):
                route = scope["path"]

            method = scope.get("method", "")
            outcome = "success" if status_code < 500 else "error"

            _http_requests_counter.add(
                1,
                {
                    "http.route": route,
                    "http.request.method": method,
                    "http.response.status_code": status_code,
                    "outcome": outcome,
                },
            )

            # Slow-request span event for P99 triage
            if elapsed > P99_BUDGET_SECONDS:
                current_span = trace.get_current_span()
                current_span.add_event(
                    "slow_request",
                    {
                        "http.route": route,
                        "http.request.method": method,
                        "http.response.status_code": status_code,
                        "duration_s": elapsed,
                        "p99_budget_s": P99_BUDGET_SECONDS,
                    },
                )


# ---------------------------------------------------------------------------
# instrument_app — called from main.py after the app is fully built
# ---------------------------------------------------------------------------

def instrument_app(application: FastAPI) -> None:
    """Attach FastAPIInstrumentor (traces + http.server.request.duration histogram)
    and the custom TelemetryMiddleware to the application.

    TelemetryMiddleware is registered BEFORE FastAPIInstrumentor.instrument_app so
    that it runs INSIDE the OTel server span — ensuring trace.get_current_span()
    returns the active span when the slow_request event is added.
    """
    _ensure_instruments()

    # Register custom middleware first so it executes inside the OTel span context
    # created by FastAPIInstrumentor (Starlette wraps in LIFO order).
    application.add_middleware(TelemetryMiddleware)

    # FastAPIInstrumentor emits http.server.request.duration (latency histogram)
    # which satisfies the P95/P99 latency SLIs and the request-rate SLI.
    FastAPIInstrumentor.instrument_app(application)
