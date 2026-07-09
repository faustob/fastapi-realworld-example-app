"""Middleware recording the request-outcome counter and slow-request span events.

This complements the FastAPIInstrumentor's automatic http.server.request.duration
histogram by emitting a low-cardinality outcome counter (route + outcome class)
for the availability SLI, active-request in-flight tracking for saturation, and
a span event when a request exceeds the P99 latency budget (750ms).
"""
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from app.core.telemetry import get_request_outcome_counter, active_requests_gauge, get_slow_request_counter

P99_BUDGET_SECONDS = 0.75


class RequestOutcomeMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        route_template = request.url.path
        if request.scope.get("route") is not None:
            route_template = getattr(request.scope["route"], "path", route_template)

        if active_requests_gauge is not None:
            active_requests_gauge.add(1, {"http.route": route_template})

        start = time.monotonic()
        try:
            response = await call_next(request)
        finally:
            if active_requests_gauge is not None:
                active_requests_gauge.add(-1, {"http.route": route_template})
        elapsed = time.monotonic() - start

        outcome_counter = get_request_outcome_counter()
        if outcome_counter is not None:
            outcome_class = "success" if response.status_code < 500 else "error"
            outcome_counter.add(
                1,
                {
                    "http.route": route_template,
                    "http.response.status_code": response.status_code,
                    "outcome": outcome_class,
                },
            )

        if elapsed > P99_BUDGET_SECONDS:
            slow_request_counter = get_slow_request_counter()
            if slow_request_counter is not None:
                slow_request_counter.add(
                    1,
                    {
                        "http.route": route_template,
                    },
                )

        return response
