import time

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from opentelemetry import trace as otel_trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from starlette.exceptions import HTTPException
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.api.errors.http_error import http_error_handler
from app.api.errors.validation_error import http422_error_handler
from app.api.routes.api import router as api_router
from app.core.config import get_app_settings
from app.core.events import create_start_app_handler, create_stop_app_handler
from app.core.telemetry import http_active_requests, setup_telemetry


async def _unhandled_exception_handler(_: Request, exc: Exception) -> JSONResponse:
    span = otel_trace.get_current_span()
    span.set_attribute("error.type", type(exc).__name__)
    span.set_status(otel_trace.Status(otel_trace.StatusCode.ERROR, str(exc)))
    return JSONResponse(status_code=500, content={"errors": ["internal_error"]})


def get_application() -> FastAPI:
    settings = get_app_settings()

    settings.configure_logging()

    setup_telemetry()

    application = FastAPI(**settings.fastapi_kwargs)

    FastAPIInstrumentor.instrument_app(application)

    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_hosts,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    slow_request_threshold_seconds = 0.75

    @application.middleware("http")
    async def _track_request_saturation_and_latency(request: Request, call_next):  # noqa: WPS430
        http_active_requests.add(1)
        start_time = time.perf_counter()
        try:
            return await call_next(request)
        finally:
            http_active_requests.add(-1)
            elapsed = time.perf_counter() - start_time
            if elapsed > slow_request_threshold_seconds:
                route = request.scope.get("route")
                route_template = getattr(route, "path", request.url.path)
                span = otel_trace.get_current_span()
                span.add_event(
                    "slow_request",
                    {"http.route": route_template, "duration_s": elapsed},
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
    application.add_exception_handler(Exception, _unhandled_exception_handler)

    application.include_router(api_router, prefix=settings.api_prefix)

    return application


app = get_application()
