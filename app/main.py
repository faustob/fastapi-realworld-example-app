from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException
from starlette.middleware.cors import CORSMiddleware

from app.api.errors.http_error import http_error_handler
from app.api.errors.validation_error import http422_error_handler
from app.api.routes.api import router as api_router
from app.core.config import get_app_settings
from app.core.events import create_start_app_handler, create_stop_app_handler
from app.core.telemetry import setup_telemetry, instrument_fastapi_app, ACTIVE_REQUESTS

import time
from starlette.requests import Request


def get_application() -> FastAPI:
    settings = get_app_settings()

    settings.configure_logging()

    setup_telemetry(service_name=settings.fastapi_kwargs.get("title", "conduit-api"))

    application = FastAPI(**settings.fastapi_kwargs)

    instrument_fastapi_app(application)

    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_hosts,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @application.middleware("http")
    async def track_active_requests(request: Request, call_next):
        ACTIVE_REQUESTS.add(1)
        try:
            response = await call_next(request)
            return response
        finally:
            ACTIVE_REQUESTS.add(-1)

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
