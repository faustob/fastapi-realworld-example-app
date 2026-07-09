import os
import time
from contextvars import ContextVar
from typing import Callable

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

_initialized = False


def init_telemetry(service_name: str = "conduit-api") -> None:
    """Build and register the global OTel SDK providers exactly once."""
    global _initialized
    if _initialized:
        return

    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")

    resource = Resource.create({SERVICE_NAME: service_name})

    tracer_provider = TracerProvider(resource=resource)
    span_exporter = OTLPSpanExporter(endpoint=endpoint) if endpoint else OTLPSpanExporter()
    tracer_provider.add_span_processor(BatchSpanProcessor(span_exporter))
    try:
        trace.set_tracer_provider(tracer_provider)
    except Exception:
        pass

    metric_exporter = OTLPMetricExporter(endpoint=endpoint) if endpoint else OTLPMetricExporter()
    metric_reader = PeriodicExportingMetricReader(metric_exporter)
    meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
    try:
        metrics.set_meter_provider(meter_provider)
    except Exception:
        pass

    _initialized = True


tracer = trace.get_tracer(__name__)
meter = metrics.get_meter(__name__)

# --- HTTP-level SLI instruments ---

request_outcome_counter = meter.create_counter(
    name="http.server.request.outcomes",
    unit="{request}",
    description="Count of HTTP requests labeled by route and outcome class",
)

active_requests_gauge = meter.create_up_down_counter(
    name="http.server.active_requests",
    unit="{request}",
    description="Number of in-flight HTTP requests",
)

worker_pool_size_gauge = meter.create_up_down_counter(
    name="http.server.worker_pool.size",
    unit="{worker}",
    description="Configured worker pool size",
)

# --- Auth SLI instruments ---

auth_attempts_counter = meter.create_counter(
    name="auth.attempts",
    unit="{attempt}",
    description="Count of authentication/authorization decisions tagged by outcome and reason",
)

# --- Business flow SLI instruments ---

flow_outcomes_counter = meter.create_counter(
    name="flow.outcomes",
    unit="{flow}",
    description="Terminal outcome count for the registration-to-publish business flow",
)

flow_duration_histogram = meter.create_histogram(
    name="flow.duration",
    unit="s",
    description="End-to-end duration of the registration-to-publish business flow",
)

flow_validation_outcomes_counter = meter.create_counter(
    name="flow.validation.outcomes",
    unit="{check}",
    description="Per-step validation outcome count within the business flow",
)

entry_to_terminal_histogram = meter.create_histogram(
    name="flow.entry_to_terminal.duration",
    unit="s",
    description="Wall-clock duration between flow entry event and terminal state transition",
)

_flow_start_var: ContextVar = ContextVar("flow_start_ts", default=None)


def start_flow_timer() -> float:
    ts = time.monotonic()
    _flow_start_var.set(ts)
    return ts


def record_flow_entry_to_terminal(flow_name: str, terminal_state: str) -> None:
    start = _flow_start_var.get()
    if start is None:
        return
    elapsed = time.monotonic() - start
    entry_to_terminal_histogram.record(elapsed, {"flow": flow_name, "terminal_state": terminal_state})
