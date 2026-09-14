"""Explicit, immutable summary-only provider controls; no model substitution."""
from dataclasses import dataclass


def reasoning_mode(value="provider_default"):
    if not isinstance(value, str) or value not in {"provider_default", "disabled", "low"}:
        raise ValueError("compaction.summary_reasoning must be provider_default, disabled or low")
    return value


def output_limit(value=8192):
    if type(value) is not int or not 1 <= value <= 393216:
        raise ValueError("compaction.summary_output_tokens must be an integer between 1 and 393216")
    return value


@dataclass(frozen=True)
class SummaryProfile:
    mode: str = "provider_default"
    provider: str = ""
    output_tokens: int = 8192

    def __post_init__(self):
        reasoning_mode(self.mode)
        output_limit(self.output_tokens)
        if self.mode != "provider_default" and self.provider != "deepseek":
            raise ValueError("summary reasoning overrides require a DeepSeek provider")

    def request_fields(self):
        if self.mode == "disabled":
            return {"thinking": {"type": "disabled"}}
        if self.mode == "low":
            return {"thinking": {"type": "enabled"}, "reasoning_effort": "low"}
        return {}


def summary_profile(settings, endpoint):
    mode = reasoning_mode(settings.get("summary_reasoning", "provider_default"))
    provider = (getattr(endpoint, "metadata", None) or {}).get("provider", "")
    return SummaryProfile(mode, provider if mode != "provider_default" else "",
                          output_limit(settings.get("summary_output_tokens", 8192)))
