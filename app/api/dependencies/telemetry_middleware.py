"""ASGI middleware recording request-outcome, active-request, and
auth-related telemetry for the Conduit API, wired into the FastAPI app in
app/main.py via app.add_middleware.
"""
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from opentelemetry import trace

from app.core import telemetry

P99_BUDGET_SECONDS = 0.75


class RequestOutcomeMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if telemetry.active_requests_updowncounter is not None:
            telemetry.active_requests_updowncounter.add(1)

        start = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            elapsed = time.perf_counter() - start

            route = request.scope.get("route")
            route_template = route.path if route is not None else request.url.path

            outcome = "success" if status_code < 500 else "failure"

            if telemetry.http_outcome_counter is not None:
                telemetry.http_outcome_counter.add(
                    1,
                    {
                        "http.route": route_template,
                        "http.request.method": request.method,
                        "outcome": outcome,
                    },
                )

            if telemetry.request_rate_counter is not None:
                tenant = request.headers.get("x-api-key", "unknown")
                telemetry.request_rate_counter.add(
                    1,
                    {
                        "http.route": route_template,
                        "tenant": tenant,
                    },
                )

            if elapsed > P99_BUDGET_SECONDS:
                span = trace.get_current_span()
                span.add_event(
                    "slow_request",
                    {
                        "http.route": route_template,
                        "duration_s": elapsed,
                        "budget_s": P99_BUDGET_SECONDS,
                    },
                )

            if telemetry.active_requests_updowncounter is not None:
                telemetry.active_requests_updowncounter.add(-1)
