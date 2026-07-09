from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException
from starlette.middleware.cors import CORSMiddleware

from app.api.errors.http_error import http_error_handler
from app.api.errors.validation_error import http422_error_handler
from app.api.routes.api import router as api_router
from app.core.config import get_app_settings
from app.core.events import create_start_app_handler, create_stop_app_handler
from app.core.telemetry import setup_telemetry

from opentelemetry import trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor


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

    @application.exception_handler(Exception)
    async def handle_unhandled_exception(request: Request, exc: Exception) -> JSONResponse:
        span = trace.get_current_span()
        span.set_attribute("error.type", type(exc).__name__)
        span.set_status(trace.StatusCode.ERROR, str(exc))
        return JSONResponse(status_code=500, content={"error": "internal_error"})

    application.include_router(api_router, prefix=settings.api_prefix)

    setup_telemetry(settings.app_name if hasattr(settings, "app_name") else "fastapi-realworld-example-app")
    FastAPIInstrumentor.instrument_app(application)

    return application


app = get_application()
