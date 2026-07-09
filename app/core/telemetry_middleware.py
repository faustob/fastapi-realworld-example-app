import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from opentelemetry import metrics, trace

meter = metrics.get_meter(__name__)
tracer = trace.get_tracer(__name__)

request_outcome_counter = meter.create_counter(
    "http.server.request.outcomes",
    unit="1",
    description="Count of HTTP requests labeled by route and outcome class",
)

request_duration_histogram = meter.create_histogram(
    "http.server.request.duration",
    unit="s",
    description="Duration of inbound HTTP requests in seconds",
)

active_requests_gauge = meter.create_up_down_counter(
    "http.server.active_requests",
    unit="{request}",
    description="Number of in-flight HTTP requests",
)

request_rate_counter = meter.create_counter(
    "http.server.requests.total",
    unit="1",
    description="Total HTTP requests, labeled by tenant/api key when available",
)

P99_BUDGET_SECONDS = 0.750


def _route_template(request: Request) -> str:
    route = request.scope.get("route")
    if route is not None and hasattr(route, "path"):
        return route.path
    return request.url.path


class TelemetryMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        route = _route_template(request)
        method = request.method

        active_requests_gauge.add(1, {"http.request.method": method})
        start_time = time.monotonic()
        try:
            with tracer.start_as_current_span(f"{method} {route}"):
                response = await call_next(request)
        finally:
            duration = time.monotonic() - start_time
            active_requests_gauge.add(-1, {"http.request.method": method})

        status_code = response.status_code
        attributes = {
            "http.request.method": method,
            "http.route": route,
            "http.response.status_code": status_code,
        }

        request_duration_histogram.record(duration, attributes)
        request_rate_counter.add(1, attributes)

        outcome = "success" if status_code < 400 else "error"
        request_outcome_counter.add(
            1,
            {
                "http.request.method": method,
                "http.route": route,
                "outcome": outcome,
            },
        )

        return response
