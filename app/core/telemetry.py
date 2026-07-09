"""OpenTelemetry SDK bootstrap and shared instrumentation helpers.

Call setup_telemetry() once at process startup (before any request is served)
and instrument_app(app) after the FastAPI application is fully constructed.
"""
from __future__ import annotations

import os
import time
from typing import Callable

from fastapi import FastAPI
from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

# ---------------------------------------------------------------------------
# SDK bootstrap
# ---------------------------------------------------------------------------

_SETUP_DONE = False


def setup_telemetry() -> None:
    """Build and register the global TracerProvider and MeterProvider.

    Safe to call multiple times (idempotent).
    """
    global _SETUP_DONE
    if _SETUP_DONE:
        return
    _SETUP_DONE = True

    service_name = os.environ.get("OTEL_SERVICE_NAME", "fastapi-realworld-example-app")
    otlp_endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")

    resource = Resource.create({"service.name": service_name})

    # --- Traces ---
    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(endpoint=otlp_endpoint + "/v1/traces")
        )
    )
    trace.set_tracer_provider(tracer_provider)

    # --- Metrics ---
    metric_reader = PeriodicExportingMetricReader(
        OTLPMetricExporter(endpoint=otlp_endpoint + "/v1/metrics")
    )
    meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
    metrics.set_meter_provider(meter_provider)


# ---------------------------------------------------------------------------
# Shared meter / instruments
# ---------------------------------------------------------------------------

def _get_meter():
    return metrics.get_meter("fastapi-realworld-example-app")


# Active-request UpDownCounter (saturation SLI)
_active_requests_counter = None


def _active_requests() -> metrics.UpDownCounter:
    global _active_requests_counter
    if _active_requests_counter is None:
        _active_requests_counter = _get_meter().create_up_down_counter(
            name="http.server.active_requests",
            unit="{request}",
            description="Number of HTTP requests currently being processed.",
        )
    return _active_requests_counter


# Auth attempt counter (auth-failure-rate SLI)
_auth_attempts_counter = None


def get_auth_attempts_counter() -> metrics.Counter:
    """Return the shared auth.attempts counter (create once)."""
    global _auth_attempts_counter
    if _auth_attempts_counter is None:
        _auth_attempts_counter = _get_meter().create_counter(
            name="auth.attempts",
            unit="{attempt}",
            description="Total authentication attempts, tagged by outcome and denial reason.",
        )
    return _auth_attempts_counter


# Flow outcome counter (e2e-flow-success / throughput SLIs)
_flow_outcomes_counter = None


def get_flow_outcomes_counter() -> metrics.Counter:
    """Return the shared flow.outcomes counter (create once)."""
    global _flow_outcomes_counter
    if _flow_outcomes_counter is None:
        _flow_outcomes_counter = _get_meter().create_counter(
            name="flow.outcomes",
            unit="{flow}",
            description="Terminal outcomes for the registration-to-publish business flow.",
        )
    return _flow_outcomes_counter


# Flow validation outcome counter (validation-failure-rate SLI)
_flow_validation_outcomes_counter = None


def get_flow_validation_outcomes_counter() -> metrics.Counter:
    """Return the shared flow.validation.outcomes counter (create once)."""
    global _flow_validation_outcomes_counter
    if _flow_validation_outcomes_counter is None:
        _flow_validation_outcomes_counter = _get_meter().create_counter(
            name="flow.validation.outcomes",
            unit="{validation}",
            description="Outcomes of per-step flow validation checks.",
        )
    return _flow_validation_outcomes_counter


# Flow entry-to-terminal duration histogram (freshness SLI)
_flow_entry_to_terminal_histogram = None


def get_flow_entry_to_terminal_histogram() -> metrics.Histogram:
    """Return the shared flow.entry_to_terminal.duration histogram (create once)."""
    global _flow_entry_to_terminal_histogram
    if _flow_entry_to_terminal_histogram is None:
        _flow_entry_to_terminal_histogram = _get_meter().create_histogram(
            name="flow.entry_to_terminal.duration",
            unit="s",
            description="Wall-clock seconds from flow entry to terminal state transition.",
        )
    return _flow_entry_to_terminal_histogram


# ---------------------------------------------------------------------------
# Active-request middleware
# ---------------------------------------------------------------------------

class ActiveRequestsMiddleware(BaseHTTPMiddleware):
    """Track in-flight HTTP requests for the saturation SLI."""

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        counter = _active_requests()
        attrs = {
            "http.request.method": request.method,
            "url.scheme": request.url.scheme,
        }
        counter.add(1, attrs)
        try:
            response = await call_next(request)
        finally:
            counter.add(-1, attrs)
        return response


# ---------------------------------------------------------------------------
# App wiring
# ---------------------------------------------------------------------------

def instrument_app(app: FastAPI) -> None:
    """Attach OTel instrumentation to a fully-constructed FastAPI application."""
    # FastAPIInstrumentor emits http.server.request.duration (latency, availability,
    # error-rate, and throughput SLIs) with the correct semconv attributes.
    FastAPIInstrumentor.instrument_app(
        app,
        tracer_provider=trace.get_tracer_provider(),
        meter_provider=metrics.get_meter_provider(),
    )
    # Active-request middleware for saturation SLI.
    app.add_middleware(ActiveRequestsMiddleware)
