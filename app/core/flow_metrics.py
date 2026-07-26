from opentelemetry import metrics, trace

tracer = trace.get_tracer("app.flow.primary")
meter = metrics.get_meter("app.flow.primary")

# Flow-entry counter: incremented every time the flow's entry point is invoked,
# independent of eventual outcome.
flow_entries_total = meter.create_counter(
    name="flow.entries.total",
    description="Count of primary business flow entries (every inbound HTTP request)",
    unit="1",
)

# Terminal-outcome counter for E2E flow success rate.
flow_outcomes_total = meter.create_counter(
    name="flow.outcomes.total",
    description="Count of primary business flow terminal outcomes, labeled by outcome",
    unit="1",
)

# E2E flow latency histogram (seconds) for P95 SLI.
flow_duration_seconds = meter.create_histogram(
    name="flow.duration.seconds",
    description="Duration of the end-to-end primary business flow",
    unit="s",
)

# Validation outcome counter for the validation-failure-rate SLI.
flow_validation_outcomes_total = meter.create_counter(
    name="flow.validation.outcomes.total",
    description="Count of primary flow validation step outcomes, labeled by outcome",
    unit="1",
)
