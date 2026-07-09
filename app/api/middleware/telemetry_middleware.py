"""ASGI middleware that tracks in-flight (active) HTTP requests.

This middleware increments/decrements the http.server.active_requests
UpDownCounter defined in app.core.telemetry, enabling the HTTP Worker Pool
Saturation SLI (avg active requests / pool size).

It is registered in app/main.py via app.add_middleware(ActiveRequestsMiddleware).
"""
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.core.telemetry import record_active_request_start, record_active_request_end


class ActiveRequestsMiddleware(BaseHTTPMiddleware):
    """Track in-flight requests for the HTTP saturation SLI."""

    async def dispatch(self, request: Request, call_next) -> Response:  # type: ignore[override]
        # Derive a low-cardinality route template where possible.
        # Starlette populates request.scope["path"] before routing; the matched
        # route template is only available after routing, so we use the raw path
        # here as a best-effort fallback — callers that need the template should
        # use the FastAPIInstrumentor-emitted http.server.request.duration instead.
        method = request.method

        record_active_request_start(method)
        try:
            response: Response = await call_next(request)
        finally:
            record_active_request_end(method)

        return response
