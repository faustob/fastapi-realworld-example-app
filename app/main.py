from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException
from starlette.middleware.cors import CORSMiddleware
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from starlette.requests import Request
from starlette.responses import JSONResponse
from app.telemetry import active_requests, setup_telemetry

from app.api.errors.http_error import http_error_handler
from app.api.errors.validation_error import http422_error_handler
from app.api.routes.api import router as api_router
from app.core.config import get_app_settings
from app.core.events import create_start_app_handler, create_stop_app_handler


def get_application() -> FastAPI:
    settings = get_app_settings()

    settings.configure_logging()

    setup_telemetry()

    application = FastAPI(**settings.fastapi_kwargs)

    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_hosts,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @application.middleware("http")
    async def track_active_requests(request: Request, call_next):
        active_requests.add(1)
        try:
            response = await call_next(request)
        finally:
            active_requests.add(-1)
        return response

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
    async def unhandled_exception_handler(req: Request, exc: Exception) -> JSONResponse:
        span = trace.get_current_span()
        span.set_attribute("error.type", type(exc).__name__)
        span.set_status(Status(StatusCode.ERROR, str(exc)))
        return JSONResponse(status_code=500, content={"detail": "Internal Server Error"})

    application.include_router(api_router, prefix=settings.api_prefix)

    FastAPIInstrumentor.instrument_app(application)

    return application


app = get_application()
