import time

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware

from app.api.errors.http_error import http_error_handler
from app.api.errors.validation_error import http422_error_handler
from app.api.routes.api import router as api_router
from app.core.config import get_app_settings
from app.core.events import create_start_app_handler, create_stop_app_handler
from app.core.telemetry import (
    setup_telemetry,
    active_requests,
    request_duration,
    request_outcomes,
)


class TelemetryMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        method = request.method
        active_requests.add(1, {"http.request.method": method})
        start = time.perf_counter()
        status_code = 500
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
            route_template = getattr(route, "path", request.url.path)
            attrs = {
                "http.request.method": method,
                "http.route": route_template,
                "url.scheme": request.url.scheme,
                "http.response.status_code": status_code,
            }
            if error_type:
                attrs["error.type"] = error_type
            request_duration.record(duration, attrs)
            outcome = "success" if status_code < 500 else "failure"
            request_outcomes.add(
                1,
                {
                    "http.route": route_template,
                    "outcome": outcome,
                    "http.response.status_code": status_code,
                },
            )
            active_requests.add(-1, {"http.request.method": method})


def get_application() -> FastAPI:
    settings = get_app_settings()

    settings.configure_logging()

    setup_telemetry(service_name="fastapi-realworld-example-app")

    application = FastAPI(**settings.fastapi_kwargs)

    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_hosts,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    application.add_middleware(TelemetryMiddleware)

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

    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    FastAPIInstrumentor.instrument_app(application)

    return application


app = get_application()
