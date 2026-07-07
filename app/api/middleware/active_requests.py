"""ASGI middleware that tracks in-flight HTTP requests and worker-pool saturation.

Emits:
  http.server.active_requests  (UpDownCounter)  — current in-flight requests
  http.server.worker_pool.size (observable gauge) — configured worker count
                                                    (reads WEB_CONCURRENCY env var,
                                                     defaults to 1 if not set)
"""
import os
from typing import Callable
from opentelemetry import metrics

_meter = metrics.get_meter("app.middleware.active_requests")

_active_requests = _meter.create_up_down_counter(
    name="http.server.active_requests",
    description="Number of HTTP requests currently being processed.",
    unit="{request}",
)


def _get_worker_pool_size() -> int:
    try:
        return int(os.environ.get("WEB_CONCURRENCY", "1"))
    except (ValueError, TypeError):
        return 1


# Observable gauge for worker pool size — registered once at module import.
_meter.create_observable_gauge(
    name="http.server.worker_pool.size",
    callbacks=[lambda options: [metrics.observation.Observation(_get_worker_pool_size())]],
    description="Configured number of Uvicorn/Gunicorn worker processes.",
    unit="{worker}",
)


class ActiveRequestsMiddleware:
    """Lightweight ASGI middleware counting in-flight requests."""

    def __init__(self, app: Callable) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        _active_requests.add(1)
        try:
            await self.app(scope, receive, send)
        finally:
            _active_requests.add(-1)
