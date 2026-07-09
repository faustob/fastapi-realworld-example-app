from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware

from opentelemetry import trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

from app.api.errors.http_error import http_error_handler
from app.api.errors.validation_error import http422_error_handler
from app.api.routes.api import router as api_router
from app.core.config import get_app_settings
from app.core.events import create_start_app_handler, create_stop_app_handler
from app.core.telemetry import (
    active_requests,
    request_outcomes,
    setup_telemetry,
)


class ActiveRequestsMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        active_requests.add(1)
        try:
            response = await call_next(request)
        finally:
            active_requests.add(-1)
        route = request.scope.get("route")
        route_template = route.path if route is not None else request.url.path
        outcome = "success" if response.status_code < 500 else "failure"
        request_outcomes.add(
            1,
            {
                "http.route": route_template,
                "http.request.method": request.method,
                "http.response.status_code": response.status_code,
                "outcome": outcome,
            },
        )
        return response


def get_application() -> FastAPI:
    settings = get_app_settings()

    settings.configure_logging()

    setup_telemetry(service_name=settings.fastapi_kwargs.get("title", "conduit"))

    application = FastAPI(**settings.fastapi_kwargs)

    FastAPIInstrumentor.instrument_app(application)

    application.add_middleware(ActiveRequestsMiddleware)

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

    @application.exception_handler(Exception)
    async def handle_unhandled_exception(request: Request, exc: Exception) -> JSONResponse:
        span = trace.get_current_span()
        span.set_attribute("error.type", type(exc).__name__)
        span.set_status(trace.StatusCode.ERROR, str(exc))
        return JSONResponse(status_code=500, content={"error": "internal_error"})

    application.include_router(api_router, prefix=settings.api_prefix)

    return application


app = get_application()
