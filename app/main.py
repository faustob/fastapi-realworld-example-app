from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException
from starlette.middleware.cors import CORSMiddleware
import time

from opentelemetry import trace, metrics
from opentelemetry.trace import Status, StatusCode
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

from app.api.errors.http_error import http_error_handler
from app.api.errors.validation_error import http422_error_handler
from app.api.routes.api import router as api_router
from app.core.config import get_app_settings
from app.core.events import create_start_app_handler, create_stop_app_handler

# ---------------------------------------------------------------------------
# OpenTelemetry SDK bootstrap — runs once at module import time
# ---------------------------------------------------------------------------
_tracer_provider = TracerProvider()
_tracer_provider.add_span_processor(
    BatchSpanProcessor(OTLPSpanExporter())  # endpoint from OTEL_EXPORTER_OTLP_ENDPOINT
)
trace.set_tracer_provider(_tracer_provider)

_metric_reader = PeriodicExportingMetricReader(OTLPMetricExporter())
_meter_provider = MeterProvider(metric_readers=[_metric_reader])
metrics.set_meter_provider(_meter_provider)

_tracer = trace.get_tracer(__name__)
_meter = metrics.get_meter(__name__)

# --- HTTP availability / throughput / error-rate counter ---
_http_requests_counter = _meter.create_counter(
    "http.server.requests",
    unit="1",
    description="Total HTTP requests by route, method, status class and outcome",
)

# --- Active-requests up-down counter (saturation) ---
_active_requests = _meter.create_up_down_counter(
    "http.server.active_requests",
    unit="1",
    description="Number of HTTP requests currently being processed",
)

# --- Request duration histogram (latency P95 / P99) ---
_request_duration = _meter.create_histogram(
    "http.server.request.duration",
    unit="s",
    description="HTTP server request duration in seconds",
)

# --- Auth attempt outcome counter ---
_auth_attempts = _meter.create_counter(
    "auth.attempts",
    unit="1",
    description="Authentication/authorization decisions tagged by outcome and reason",
)

# --- Flow outcome counter ---
_flow_outcomes = _meter.create_counter(
    "flow.outcomes",
    unit="1",
    description="Terminal outcomes for the registration-to-publish business flow",
)

# --- Flow entry counter (throughput) ---
_flow_entries = _meter.create_counter(
    "flow.entries",
    unit="1",
    description="Number of times the primary business flow entry point is invoked",
)

# --- Flow validation outcome counter ---
_flow_validation_outcomes = _meter.create_counter(
    "flow.validation.outcomes",
    unit="1",
    description="Per-step validation pass/fail outcomes for the primary flow",
)

# --- Entry-to-terminal duration histogram (freshness) ---
_flow_entry_to_terminal = _meter.create_histogram(
    "flow.entry_to_terminal.duration",
    unit="s",
    description="Wall-clock time from flow entry to terminal state transition",
)

# --- Flow duration histogram (E2E latency P95) ---
_flow_duration = _meter.create_histogram(
    "flow.duration",
    unit="s",
    description="End-to-end duration of the primary business flow",
)

# P99 budget in seconds — requests exceeding this get a span event
_P99_BUDGET_S = 0.750


def get_application() -> FastAPI:
    settings = get_app_settings()

    settings.configure_logging()

    application = FastAPI(**settings.fastapi_kwargs)

    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_hosts,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    application.add_event_handler(
        "startup",
        create_start_app_handler(application, settings),
    )
    application.add_event_handler(
        "shutdown",
        create_stop_app_handler(application),
    )

    application.add_exception_handler(HTTPException, http_error_handler)
    application.add_exception_handler(RequestValidationError, http422_error_handler)

    application.include_router(api_router, prefix=settings.api_prefix)

    # ------------------------------------------------------------------
    # Middleware: active-request gauge + duration histogram + outcome counter
    # ------------------------------------------------------------------
    @application.middleware("http")
    async def _otel_metrics_middleware(request: Request, call_next):
        route = request.url.path
        method = request.method
        _active_requests.add(1, {"http.request.method": method, "http.route": route})
        start = time.perf_counter()
        current_span = trace.get_current_span()
        try:
            response = await call_next(request)
            elapsed = time.perf_counter() - start
            status_code = response.status_code
            outcome = "success" if status_code < 500 else "error"
            status_class = f"{status_code // 100}xx"
            attrs = {
                "http.request.method": method,
                "http.route": route,
                "http.response.status_code": status_code,
                "http.status_class": status_class,
                "outcome": outcome,
            }
            _request_duration.record(elapsed, attrs)
            _http_requests_counter.add(1, attrs)
            # Slow-request span event for P99 triage
            if elapsed > _P99_BUDGET_S and current_span.is_recording():
                current_span.add_event(
                    "slow_request",
                    {
                        "http.route": route,
                        "http.request.method": method,
                        "duration_s": elapsed,
                        "p99_budget_s": _P99_BUDGET_S,
                    },
                )
            # Exception-to-status mapping: set error attributes on the active span
            if status_code >= 500 and current_span.is_recording():
                current_span.set_attribute("error.type", "HTTPError")
                current_span.set_status(
                    Status(StatusCode.ERROR, f"HTTP {status_code}")
                )
            return response
        finally:
            _active_requests.add(-1, {"http.request.method": method, "http.route": route})

    # ------------------------------------------------------------------
    # Wire FastAPI auto-instrumentation (traces + http.server.request.duration)
    # ------------------------------------------------------------------
    FastAPIInstrumentor.instrument_app(application, tracer_provider=_tracer_provider)

    return application


app = get_application()
