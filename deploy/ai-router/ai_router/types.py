from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class RequestCapabilities:
    protocol: str
    tools: bool = False
    parallel_tools: bool = False
    tool_choice: bool = False
    structured_output: str | None = None
    streaming: bool = False

    def labels(self) -> tuple[str, ...]:
        values = [self.protocol]
        if self.tools:
            values.append("tools")
        if self.parallel_tools:
            values.append("parallel_tools")
        if self.tool_choice:
            values.append("tool_choice")
        if self.structured_output:
            values.append(self.structured_output)
        if self.streaming:
            values.append("streaming")
        return tuple(values)


@dataclass(frozen=True)
class EndpointCapabilities:
    chat: bool = True
    responses: str = "none"
    tools: str = "none"
    tool_choice: bool = False
    structured_output: tuple[str, ...] = ()
    streaming: bool = True
    validation_status: str = "unverified"
    validated_at: str = ""

    def supports(self, required: RequestCapabilities) -> bool:
        if required.protocol == "chat" and not self.chat:
            return False
        if required.protocol == "responses" and self.responses == "none":
            return False
        if required.tools and self.tools == "none":
            return False
        if required.parallel_tools and self.tools != "parallel":
            return False
        if required.tool_choice and not self.tool_choice:
            return False
        if (
            required.structured_output
            and required.structured_output not in self.structured_output
        ):
            return False
        if required.streaming and not self.streaming:
            return False
        return True

    def protocol_mode(self, protocol: str) -> str:
        if protocol == "responses":
            return self.responses
        return "native"


@dataclass(frozen=True)
class Endpoint:
    id: str
    public_model: str
    provider_model: str
    api_base: str
    node: str
    role: str
    tier: str
    tier_rank: int
    modalities: tuple[str, ...]
    tasks: tuple[str, ...]
    safe_context_tokens: int
    configured_context_tokens: int
    max_concurrency: int
    backend_type: str
    health_url: str
    load_url: str | None = None
    backend_api_key_env: str = "AI_ROUTER_BACKEND_API_KEY"
    enabled: bool = True
    auto_candidate: bool = True
    cloud: bool = False
    capabilities: EndpointCapabilities = field(
        default_factory=EndpointCapabilities
    )
    quality: dict[str, float] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def tools(self) -> bool:
        return self.capabilities.tools != "none"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EndpointStatus:
    endpoint_id: str
    healthy: bool
    checked_at: float
    load_headroom: float = 1.0
    latency_score: float = 0.5
    cache_generation: str = ""
    eligible_context_tokens: int | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def is_fresh(self, now: float, stale_after_seconds: float) -> bool:
        return self.healthy and now - self.checked_at <= stale_after_seconds

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "EndpointStatus":
        return cls(**value)


@dataclass
class ConversationState:
    conversation_id: str
    public_model: str
    endpoint_id: str
    tier_rank: int
    task: str
    last_seen: float
    cache_generation: str = ""
    deployment_id: str | None = None
    upstream_api_base: str | None = None
    encrypted_capsule: str | None = None
    boundary_hash: str | None = None
    migration_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ConversationState":
        return cls(**value)


@dataclass
class RouteDecision:
    endpoint: Endpoint
    requested_model: str
    task: str
    prompt_tokens: int
    output_reserve_tokens: int
    reason: str
    affinity: str
    score: float
    migration: bool = False
    previous_endpoint_id: str | None = None
    deployment_id: str | None = None
    upstream_api_base: str | None = None
    deployment_candidates: tuple[tuple[str, str], ...] = ()
    attempts: int = 1
    capacity_attempts: int = 1
    queue_wait_ms: float = 0.0
    protocol: str = "chat"
    native_or_adapter: str = "native"
    required_capabilities: tuple[str, ...] = ()
    tool_history_repairs: int = 0
    candidate_rejections: tuple[str, ...] = ()

    def response_headers(self, request_id: str) -> dict[str, str]:
        values = {
            "X-1Panel-Route-Request-ID": request_id,
            "X-1Panel-Route-Node": self.endpoint.node,
            "X-1Panel-Route-Model": self.endpoint.public_model,
            "X-1Panel-Route-Deployment": self.deployment_id or self.endpoint.id,
            "X-1Panel-Route-Reason": self.reason,
            "X-1Panel-Prompt-Tokens": str(self.prompt_tokens),
            "X-1Panel-Affinity": self.affinity,
            "X-1Panel-Route-Attempts": str(self.attempts),
            "X-1Panel-Capacity-Attempts": str(self.capacity_attempts),
            "X-1Panel-Queue-Wait-Ms": str(round(self.queue_wait_ms, 2)),
            "X-1Panel-Protocol": self.protocol,
            "X-1Panel-Protocol-Mode": self.native_or_adapter,
        }
        if self.tool_history_repairs:
            values["X-1Panel-Tool-History-Repaired"] = str(
                self.tool_history_repairs
            )
        return values


@dataclass(frozen=True)
class ClientPolicy:
    id: str
    key_env: str
    models: tuple[str, ...]
    rpm_limit: int
    tpm_limit: int
    max_parallel_requests: int


@dataclass
class Evaluation:
    task: str
    required_tier: str | None
    confidence: float
    reason: str
    preferred_tier: str | None = None
