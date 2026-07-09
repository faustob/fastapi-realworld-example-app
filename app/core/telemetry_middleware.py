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
    route = request.scope.get(