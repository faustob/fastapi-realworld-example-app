import time

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException
from starlette.middleware.cors import CORSMiddleware

from app.api.errors.http_error import http_error_handler
from app.api.errors.validation_error import http422_error_handler
from app.api.routes.api import router as api_router
from app.core.config import get_app_settings
from app.core.events import create_start_app_handler, create_stop_app_handler
from app.core.telemetry import setup_telemetry, get_meter, get_tracer

setup_telemetry()

meter = get_meter(__name__)
tracer = get_tracer(__name__)

request_duration_histogram = meter.create_histogram(
    "http.server.request.duration",
    unit="s",
    description="Duration of inbound HTTP requests",
)
active_requests_gauge = meter.create_up_down_counter(
    "http.server.active_requests",
    unit="{request}",
    description="Number of in-flight HTTP requests",
)
request_rate_counter = meter.create_counter(
    "http.server.request.count",
    unit="{request}",
    description="Total inbound HTTP requests, tagged by tenant/api key",
)

P99_LATENCY_BUDGET_SECONDS = 0.75


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

    @application.middleware("http")
    async def telemetry_middleware(request: Request, call_next):
        method = request.method
        scheme = request.url.scheme
        active_requests_gauge.add(1, {"http.request.method": method})
        start = time.perf_counter()
        status_code = None
        error_type = None
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        except Exception as exc:
            error_type = type(exc).__name__
            raise
        finally:
            duration = time.perf_counter() - start
            route = request.scope.get("route")
            route_template = route.path if route is not None else request.url.path
            attributes = {
                "http.request.method": method,
                "url.scheme": scheme,
                "http.route": route_template,
            }
            if status_code is not None:
                attributes["http.response.status_code"] = status_code
            if error_type is not None:
                attributes["error.type"] = error_type
            request_duration_histogram.record(duration, attributes)
            active_requests_gauge.add(-1, {"http.request.method": method})
            has_auth_header = "Authorization" in request.headers
            request_rate_counter.add(
                1,
                {
                    "http.route": route_template,
                    "http.request.method": method,
                    "client.authenticated": has_auth_header,
                },
            )
            if duration > P99_LATENCY_BUDGET_SECONDS:
                span = tracer.start_span("slow_request")
                span.add_event(
                    "slow_request_exceeded_p99_budget",
                    {
                        "http.route": route_template,
                        "http.request.method": method,
                        "duration_seconds": duration,
                    },
                )
                span.end()

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

    return application


app = get_application()
