"""ASGI middleware that tracks in-flight HTTP requests for the saturation SLI.

This middleware increments/decrements the http.server.active_requests
UpDownCounter around every request so the Uvicorn worker-pool saturation
SLI can be computed as:

    avg(http.server.active_requests) / http.server.worker_pool.size
"""
from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.core.sli_metrics import http_active_requests


class ActiveRequestsMiddleware(BaseHTTPMiddleware):
    """Track in-flight requests for the HTTP saturation SLI."""

    async def dispatch(self, request: Request, call_next) -> Response:  # type: ignore[override]
        http_active_requests.add(1, {"http.request.method": request.method})
        try:
            response = await call_next(request)
            return response
        finally:
            http_active_requests.add(-1, {"http.request.method": request.method})
