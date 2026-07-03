"""ASGI middleware that records HTTP SLI metrics for every request."""
from __future__ import annotations

import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from app.telemetry import (
    http_active_requests,
    http_requests_total,
    record_http_request,
)


class TelemetryMiddleware(BaseHTTPMiddleware):
    """Records http.server.requests.total and http.server.active_requests."""

    async def dispatch(self, request: Request, call_next: object) -> Response:
        route = request.scope.get("path", request.url.path)
        method = request.method

        active_attrs = {"http.request.method": method, "http.route": route}
        http_active_requests.add(1, active_attrs)
        start = time.perf_counter()
        status_code = 500
        try:
            response: Response = await call_next(request)  # type: ignore[arg-type]
            status_code = response.status_code
        finally:
            duration_s = time.perf_counter() - start
            http_active_requests.add(-1, active_attrs)

        record_http_request(method, route, status_code, duration_s)

        # Attach exception.type to the current span for 5xx attribution
        if status_code >= 500:
            span = trace.get_current_span()
            span.set_attribute("http.response.status_code", status_code)
            span.set_status(Status(StatusCode.ERROR, f"HTTP {status_code}"))

        return response
