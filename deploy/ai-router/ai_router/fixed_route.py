from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .errors import RouteDirectiveIncompatibleError, RouteDirectiveUnavailableError


@dataclass(frozen=True)
class FixedRouteIntent:
    """A server-derived, single-endpoint route constraint."""

    endpoint_id: str
    source: str


def resolve_fixed_route_intent(
    registry: Any,
    *,
    requested_model: str,
    route_resolution: Any | None,
    directive: Any | None,
    conversation_control: dict[str, Any] | None,
) -> FixedRouteIntent | None:
    """Resolve only trusted fixed routes; client headers are not inputs."""
    if (conversation_control or {}).get("pin"):
        return None
    bound_id = str(route_resolution.target_endpoint_id) if route_resolution is not None else None
    directed_id = (
        str(directive.endpoint_id)
        if directive is not None and directive.endpoint_id
        else None
    )
    if bound_id and directed_id and bound_id != directed_id:
        raise RouteDirectiveIncompatibleError(
            "the route directive conflicts with the client route binding"
        )
    endpoint_id = directed_id or bound_id
    source = "route_directive" if directed_id else "client_route_binding"
    if endpoint_id:
        endpoint = registry.by_id(endpoint_id)
        if endpoint is None or endpoint.role != "responder":
            raise RouteDirectiveUnavailableError(
                "the directed model endpoint is not registered"
            )
        return FixedRouteIntent(endpoint.id, source)
    if requested_model == "auto":
        return None
    endpoints = tuple(
        endpoint
        for endpoint in registry.by_public_model(requested_model)
        if endpoint.role == "responder"
    )
    if len(endpoints) != 1:
        return None
    return FixedRouteIntent(endpoints[0].id, "explicit_model")
