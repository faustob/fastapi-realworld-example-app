from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from opentelemetry import trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

from app.api.errors.http_error import http_error_handler
from app.api.errors.validation_error import http422_error_handler
from app.api.routes.api import router as api_router
from app.core.config import get_app_settings
from app.core.events import create_start_app_handler, create_stop_app_handler
from app.core.telemetry import init_telemetry, request_outcome_counter


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    span = trace.get_current_span()
    span.set_attribute("error.type", type(exc).__name__)
    span.set_status(trace.StatusCode.ERROR, str(exc))
    request_outcome_counter.add(
        1,
        {
            "http.route": request.scope.get("route").path
            if request.scope.get("route")
            else request.url.path,
            "outcome": "error",
            "error.type": type(exc).__name__,
        },
    )
    return JSONResponse(status_code=500, content={"errors": {"body": ["internal server error"]}})


def get_application() -> FastAPI:
    settings = get_app_settings()

    settings.configure_logging()

    init_telemetry(service_name=settings.fastapi_kwargs.get("title", "conduit-api"))

    application = FastAPI(**settings.fastapi_kwargs)

    FastAPIInstrumentor.instrument_app(application)

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
    application.add_exception_handler(Exception, unhandled_exception_handler)

    application.include_router(api_router, prefix=settings.api_prefix)

    return application


app = get_application()
