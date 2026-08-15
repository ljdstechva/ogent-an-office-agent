"""Composed standard-library HTTP handler for Ogent."""

from __future__ import annotations

from .http_foundation import (
    OgentHandlerFoundation,
    bind_runtime as bind_foundation_runtime,
)
from .http_get_routes import (
    OgentGetRoutesMixin,
    bind_runtime as bind_get_runtime,
)
from .http_post_routes import (
    OgentPostRoutesMixin,
    bind_runtime as bind_post_runtime,
)


def bind_http_runtime(values: dict[str, object]) -> None:
    bind_foundation_runtime(values)
    bind_get_runtime(values)
    bind_post_runtime(values)


class RuntimeBoundOgentHandler(
    OgentPostRoutesMixin,
    OgentGetRoutesMixin,
    OgentHandlerFoundation,
):
    """Synchronize compatibility globals before each request."""

    runtime_values: dict[str, object] = {}

    def handle_one_request(self) -> None:
        bind_http_runtime(self.runtime_values)
        super().handle_one_request()
