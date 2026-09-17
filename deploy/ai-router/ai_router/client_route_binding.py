from __future__ import annotations

import codecs
import json
from dataclasses import dataclass
from typing import Any, Mapping

from .errors import RouteDirectiveIncompatibleError, RouterError
from .types import Evaluation


@dataclass(frozen=True)
class ClientRouteResolution:
    """Canonical route intent derived from a legacy client contract."""

    client_id: str
    requested_model: str
    requested_tier: str | None
    target_model: str
    target_endpoint_id: str
    ignored_route_tiers: frozenset[str]

    def normalized_headers(
        self,
        headers: Mapping[str, str],
    ) -> dict[str, str]:
        normalized = dict(headers)
        tier = normalized.get("x-1panel-route-tier", "").strip().lower()
        if tier in self.ignored_route_tiers:
            normalized.pop("x-1panel-route-tier", None)
        return normalized

    def apply_to_evaluation(self, evaluation: Evaluation) -> None:
        evaluation.required_endpoint_id = self.target_endpoint_id
        evaluation.required_endpoint_source = "client_route_binding"
        evaluation.evidence = {
            **evaluation.evidence,
            "client_route_resolution": self.target_endpoint_id,
        }

    def ensure_directive_compatible(
        self,
        endpoint_id: str | None,
    ) -> None:
        if endpoint_id and endpoint_id != self.target_endpoint_id:
            raise RouteDirectiveIncompatibleError(
                "the route directive conflicts with the client route "
                "binding"
            )

    def audit_metadata(self) -> dict[str, Any]:
        metadata: dict[str, Any] = {
            "requested_model": self.requested_model,
            "resolved_model": self.target_model,
            "target_endpoint_id": self.target_endpoint_id,
        }
        if self.requested_tier in self.ignored_route_tiers:
            metadata["replaced_constraints"] = {
                "route_tier": self.requested_tier,
            }
        return metadata


def resolve_client_route(
    routing: dict[str, Any],
    registry: Any,
    *,
    client_id: str,
    requested_model: str,
    disclosure_mode: str,
    headers: Mapping[str, str] | None = None,
) -> ClientRouteResolution | None:
    if disclosure_mode != "internal":
        return None
    for rule in routing.get("client_route_bindings", []):
        if (
            rule["client_id"] != client_id
            or requested_model not in rule["requested_models"]
        ):
            continue
        endpoint_id = rule["target_endpoint_id"]
        endpoint = registry.by_id(endpoint_id)
        if endpoint is None or endpoint.role != "responder":
            raise _invalid_target(endpoint_id)
        return ClientRouteResolution(
            client_id=client_id,
            requested_model=requested_model,
            requested_tier=(
                (headers or {}).get("x-1panel-route-tier", "")
                .strip()
                .lower()
                or None
            ),
            target_model=endpoint.public_model,
            target_endpoint_id=endpoint.id,
            ignored_route_tiers=frozenset(rule["ignored_route_tiers"]),
        )
    return None


def rewrite_response_model(payload: bytes, model: str | None) -> bytes:
    if not model or not payload:
        return payload
    try:
        value = json.loads(payload)
    except (TypeError, ValueError):
        raise RouterError(
            "the model returned an invalid response",
            status_code=502,
            code="invalid_upstream_response",
        ) from None
    if not isinstance(value, dict):
        raise RouterError(
            "the model returned an invalid response",
            status_code=502,
            code="invalid_upstream_response",
        )
    _rewrite_model_fields(value, model)
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


class SSEModelRewriter:
    def __init__(self, model: str | None) -> None:
        self.model = model
        self._decoder = codecs.getincrementaldecoder("utf-8")(
            errors="replace"
        )
        self._buffer = ""

    def feed(self, chunk: bytes) -> list[bytes]:
        if not self.model:
            return [chunk]
        self._buffer += self._decoder.decode(chunk)
        return self._consume(final=False)

    def finish(self) -> list[bytes]:
        if not self.model:
            return []
        self._buffer += self._decoder.decode(b"", final=True)
        return self._consume(final=True)

    def _consume(self, *, final: bool) -> list[bytes]:
        lines = self._buffer.splitlines(keepends=True)
        self._buffer = ""
        output: list[bytes] = []
        for line in lines:
            if not final and not line.endswith(("\n", "\r")):
                self._buffer = line
                continue
            stripped = line.strip()
            if not stripped.startswith("data:") or stripped == "data: [DONE]":
                output.append(line.encode("utf-8"))
                continue
            raw = stripped[5:].strip()
            try:
                value = json.loads(raw)
            except (TypeError, ValueError):
                raise RouterError(
                    "the model returned an invalid stream",
                    status_code=502,
                    code="invalid_upstream_response",
                ) from None
            if not isinstance(value, dict):
                raise RouterError(
                    "the model returned an invalid stream",
                    status_code=502,
                    code="invalid_upstream_response",
                )
            _rewrite_model_fields(value, self.model)
            ending = (
                "\r\n"
                if line.endswith("\r\n")
                else "\n"
                if line.endswith("\n")
                else "\r"
                if line.endswith("\r")
                else ""
            )
            output.append(
                (
                    "data: "
                    + json.dumps(
                        value,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    + ending
                ).encode("utf-8")
            )
        return output


def _rewrite_model_fields(value: dict[str, Any], model: str) -> None:
    if "model" in value:
        value["model"] = model
    response = value.get("response")
    if isinstance(response, dict) and "model" in response:
        response["model"] = model


def _invalid_target(endpoint_id: str) -> RouterError:
    return RouterError(
        "client route binding target is unavailable",
        status_code=503,
        code="client_route_binding_invalid",
        details={"target_endpoint_id": endpoint_id},
    )
