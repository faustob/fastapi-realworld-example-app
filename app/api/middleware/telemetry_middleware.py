"""ASGI middleware that tracks in-flight requests for the saturation SLI.

This middleware increments/decrements the http.server.active_requests
up-down counter around every request so the saturation SLI
(active_requests / worker_pool_size) can be computed from metrics.
"""
from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.core import telemetry


class ActiveRequestsMiddleware(BaseHTTPMiddleware):
    """Track in-flight HTTP requests via an OTel UpDownCounter."""

    async def dispatch(self, request: Request, call_next) -> Response:  # type: ignore[override]
        if telemetry.active_requests_counter is not None:
            telemetry.active_requests_counter.add(1, {"http.request.method": request.method})
        try:
            response = await call_next(request)
        finally:
            if telemetry.active_requests_counter is not None:
                telemetry.active_requests_counter.add(-1, {"http.request.method": request.method})
        return response
