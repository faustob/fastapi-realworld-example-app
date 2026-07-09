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
from app.core.telemetry import (
    active_requests,
    http_request_duration,
    request_outcome_counter,
    setup_telemetry,
    tracer,
)
from opentelemetry import trace as otel_trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor


def get_application() -> FastAPI:
    settings = get_app_settings()

    settings.configure_logging()

    setup_telemetry(settings)

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
        start = time.perf_counter()
        active_requests.add(1)
        status_code = 500
        error_type = None
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        except Exception as exc:
            error_type = type(exc).__name__
            span = otel_trace.get_current_span()
            span.set_attribute("error.type", error_type)
            span.set_status(otel_trace.StatusCode.ERROR, str(exc))
            raise
        finally:
            duration = time.perf_counter() - start
            active_requests.add(-1)
            route = request.scope.get("route")
            route_template = route.path if route is not None else request.url.path
            attributes = {
                "http.request.method": request.method,
                "http.route": route_template,
                "url.scheme": request.url.scheme,
                "http.response.status_code": status_code,
            }
            if error_type:
                attributes["error.type"] = error_type
            http_request_duration.record(duration, attributes)
            outcome = "success" if status_code < 500 else "error"
            request_outcome_counter.add(
                1,
                {
                    "http.route": route_template,
                    "http.request.method": request.method,
                    "outcome": outcome,
                },
            )
            if duration > 0.75:
                span = otel_trace.get_current_span()
                span.add_event(
                    "slow_request",
                    {
                        "duration_s": duration,
                        "http.route": route_template,
                    },
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

    FastAPIInstrumentor.instrument_app(application)

    return application


app = get_application()
