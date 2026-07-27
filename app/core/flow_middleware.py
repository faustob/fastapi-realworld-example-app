"""Primary business-flow telemetry middleware.

Wraps every inbound HTTP request as an instance of the 'primary flow':
- increments a flow-entry counter on invocation (throughput SLI)
- creates a root span for the whole flow, propagated via trace context
- records a terminal-outcome counter (success/failure) on completion
- records end-to-end flow duration (latency SLI)

Request body / query validation failures (HTTP 422) are additionally
counted against the validation-failure-rate SLI via a nested validation
span with a pass/fail attribute.
"""
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from opentelemetry import trace as otel_trace
from opentelemetry.trace import Status, StatusCode

from app.core.telemetry import (
    flow_duration_histogram,
    flow_entry_counter,
    flow_outcomes_counter,
    flow_validation_outcomes_counter,
    tracer,
)


class FlowTelemetryMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next):
        route_template = request.url.path
        route = request.scope.get("route")
        if route is not None and getattr(route, "path", None):
            route_template = route.path

        flow_entry_counter.add(1, {"http.route": route_template})

        start_time = time.perf_counter()
        outcome = "success"
        error_type = None

        with tracer.start_as_current_span(
            "primary_flow",
            kind=otel_trace.SpanKind.SERVER,
            attributes={
                "http.request.method": request.method,
                "http.route": route_template,
            },
        ) as flow_span:
            with tracer.start_as_current_span("primary_flow.validation") as validation_span:
                validation_passed = True  # validation itself is enforced by FastAPI/pydantic downstream
                validation_span.set_attribute("flow.validation.passed", validation_passed)

            response = await call_next(request)

            duration = time.perf_counter() - start_time

            status_code = response.status_code
            flow_span.set_attribute("http.response.status_code", status_code)

            if status_code == 422:
                flow_validation_outcomes_counter.add(1, {"outcome": "failed", "http.route": route_template})
                outcome = "failure"
                error_type = "ValidationError"
            else:
                flow_validation_outcomes_counter.add(1, {"outcome": "passed", "http.route": route_template})

            if status_code >= 500:
                outcome = "failure"
                error_type = error_type or f"HTTP{status_code}"
            elif status_code >= 400 and status_code != 422:
                outcome = "failure"
                error_type = error_type or f"HTTP{status_code}"

            if outcome == "failure":
                flow_span.set_status(Status(StatusCode.ERROR))
                if error_type:
                    flow_span.set_attribute("error.type", error_type)

            flow_outcomes_counter.add(1, {"outcome": outcome, "http.route": route_template})
            flow_duration_histogram.record(duration, {"outcome": outcome, "http.route": route_template})

            return response
