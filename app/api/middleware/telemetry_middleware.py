"""ASGI middleware that tracks active in-flight HTTP requests.

This middleware increments/decrements the http.server.active_requests
UpDownCounter so the HTTP Worker Pool Saturation SLI can be computed.
FastAPIInstrumentor already emits http.server.request.duration; this
middleware only adds the concurrency gauge.
"""
from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from app.telemetry import active_requests_gauge


class ActiveRequestsMiddleware(BaseHTTPMiddleware):
    """Track the number of concurrently in-flight HTTP requests."""

    async def dispatch(self, request: Request, call_next):
        if active_requests_gauge is not None:
            active_requests_gauge.add(1, {"http.request.method": request.method})
        try:
            response = await call_next(request)
        finally:
            if active_requests_gauge is not None:
                active_requests_gauge.add(-1, {"http.request.method": request.method})
        return response
