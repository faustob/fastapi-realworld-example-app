import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from app.core.flow_metrics import (
    flow_duration_seconds,
    flow_entries_total,
    flow_outcomes_total,
    flow_validation_outcomes_total,
    tracer,
)


class PrimaryFlowMiddleware(BaseHTTPMiddleware):
    """Wraps every inbound request as an instance of the primary business flow.

    Emits a flow-entry counter on every invocation, a root flow span propagated
    via trace context, a terminal-outcome counter (success/failure), a
    validation-outcome counter (for 4xx client-side validation errors), and an
    end-to-end flow duration histogram.
    """

    async def dispatch(self, request: Request, call_next):
        route = request.scope.get("route")
        route_template = route.path if route is not None else request.url.path

        flow_entries_total.add(1, {"http.route": route_template})

        start_time = time.monotonic()

        with tracer.start_as_current_span("primary_flow") as span:
            span.set_attribute("http.route", route_template)
            span.set_attribute("http.request.method", request.method)

            try:
                response = await call_next(request)
            except Exception:
                duration = time.monotonic() - start_time
                flow_duration_seconds.record(duration, {"http.route": route_template, "flow.outcome": "failure"})
                flow_outcomes_total.add(1, {"http.route": route_template, "flow.outcome": "failure"})
                span.set_attribute("flow.outcome", "failure")
                raise

            duration = time.monotonic() - start_time
            status_code = response.status_code

            if status_code < 400:
                outcome = "success"
            elif status_code == 422 or status_code == 400:
                outcome = "failure"
                flow_validation_outcomes_total.add(
                    1, {"http.route": route_template, "flow.outcome": "validation_failure"}
                )
            else:
                outcome = "failure"

            flow_duration_seconds.record(duration, {"http.route": route_template, "flow.outcome": outcome})
            flow_outcomes_total.add(1, {"http.route": route_template, "flow.outcome": outcome})
            span.set_attribute("flow.outcome", outcome)
            span.set_attribute("http.response.status_code", status_code)

        return response
