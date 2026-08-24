from fastapi import HTTPException
from opentelemetry import trace
from starlette.requests import Request
from starlette.responses import JSONResponse


async def http_error_handler(_: Request, exc: HTTPException) -> JSONResponse:
    if exc.status_code >= 500:
        span = trace.get_current_span()
        span.set_attribute("error.type", type(exc).__name__)
        span.set_status(trace.StatusCode.ERROR, str(exc.detail))
    return JSONResponse({"errors": [exc.detail]}, status_code=exc.status_code)
