"""OpenTelemetry bootstrap and shared instruments for the primary business flow.

This service runs as a single uvicorn process with no pre-fork workers
(see the Dockerfile CMD), so registering the global providers at module
import time is safe. The module is imported for its side effect from
app/main.py (and transitively via app/core/events.py) before the FastAPI
application is built/serves traffic.

Exposes the counters/histogram/spans used to measure the primary
business flow (user registration -> login -> first article publish):
  * flow.entries                 - flow entry-point invocations
  * flow.outcomes                - terminal success/failure outcome
  * flow.duration                - end-to-end duration (seconds)
  * flow.validation.outcomes     - per-step validation pass/fail
"""
from __future__ import annotations

import os
import time
from contextlib import contextmanager
from typing import Dict, Iterator, Tuple

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Span

_SERVICE_NAME = os.environ.get("OTEL_SERVICE_NAME", "conduit-api")


def _init_telemetry() -> Tuple[TracerProvider, MeterProvider]:
    """Build and register the global OTel SDK providers exactly once.

    `set_tracer_provider`/`set_meter_provider` never raise on
    re-registration (they log and keep whichever provider is already
    installed), so this is safe to call at import time whether or not
    an external agent has already registered a provider.
    """
    resource = Resource.create({"service.name": _SERVICE_NAME})

    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(tracer_provider)

    metric_reader = PeriodicExportingMetricReader(OTLPMetricExporter())
    meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
    metrics.set_meter_provider(meter_provider)

    return tracer_provider, meter_provider


_tracer_provider, _meter_provider = _init_telemetry()


def shutdown_telemetry() -> None:
    """Flush and shut down the providers built by this module.

    Called from the app's shutdown handler so buffered spans
    (BatchSpanProcessor) and the final metrics export
    (PeriodicExportingMetricReader) aren't lost when the process exits.
    """
    _tracer_provider.shutdown()
    _meter_provider.shutdown()


tracer = trace.get_tracer("app.flow.primary")
meter = metrics.get_meter("app.flow.primary")

flow_entries_total = meter.create_counter(
    name="flow.entries",
    unit="1",
    description="Invocations of the primary business flow's entry points (registration, article publish), regardless of eventual outcome",
)

flow_outcomes_total = meter.create_counter(
    name="flow.outcomes",
    unit="1",
    description="Terminal outcome (success/failure) of the primary business flow (registration-to-first-article-publish)",
)

flow_duration_seconds = meter.create_histogram(
    name="flow.duration",
    unit="s",
    description="End-to-end duration, in seconds, of the primary business flow's terminal step",
)

flow_validation_outcomes_total = meter.create_counter(
    name="flow.validation.outcomes",
    unit="1",
    description="Outcome (passed/failed) of validation steps within the primary business flow",
)


@contextmanager
def flow_outcome(step: str) -> Iterator[Span]:
    """Root span + terminal-outcome counter + duration histogram for a flow step.

    Any exception raised inside the `with` block is recorded as a
    failure outcome and re-raised unchanged (never swallowed); success
    is recorded only if the block completes without raising.
    """
    start = time.monotonic()
    outcome = "success"
    with tracer.start_as_current_span(f"primary_flow.{step}") as span:
        span.set_attribute("flow.step", step)
        try:
            yield span
        except Exception:
            outcome = "failure"
            raise
        finally:
            duration = time.monotonic() - start
            span.set_attribute("flow.outcome", outcome)
            flow_outcomes_total.add(1, {"outcome": outcome, "flow.step": step})
            flow_duration_seconds.record(duration, {"flow.step": step})


@contextmanager
def validation_step(step: str) -> Iterator[Dict[str, bool]]:
    """Nested validation span for one step of the flow's validation pipeline.

    Callers set `result["failed"] = True` before raising/returning to
    mark the step failed; the outcome is always recorded in `finally`,
    so it is captured whether or not the caller raises.
    """
    result: Dict[str, bool] = {"failed": False}
    with tracer.start_as_current_span(f"validation.{step}") as span:
        try:
            yield result
        finally:
            outcome = "failed" if result["failed"] else "passed"
            span.set_attribute("validation.outcome", outcome)
            span.set_attribute("validation.step", step)
            flow_validation_outcomes_total.add(1, {"outcome": outcome, "validation.step": step})
