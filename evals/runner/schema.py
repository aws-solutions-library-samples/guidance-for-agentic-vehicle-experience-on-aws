"""Pydantic v2 schema for eval case YAML files."""

from typing import Any, Literal

from pydantic import BaseModel, model_validator


class Input(BaseModel):
    """Input data for an eval case (text utterance, audio file, or structured args).
    
    Attributes:
        type: Either 'text' or 'audio'.
        utterance: Natural language input (for text-input cases).
        audio_path: Relative path to a WAV file in evals/audio/ (for audio-input cases).
        input_args: Structured kwargs for the tool (used by Tier 1 multi-parameter tools).
    """
    type: Literal["text", "audio"]
    utterance: str | None = None
    audio_path: str | None = None
    input_args: dict[str, Any] | None = None

    @model_validator(mode="after")
    def require_utterance_or_audio(self) -> "Input":
        if self.utterance is None and self.audio_path is None and self.input_args is None:
            raise ValueError("Input requires at least one of: utterance, audio_path, input_args")
        return self


class Expected(BaseModel):
    """Expected outputs and assertions for an eval case.
    
    Attributes:
        tool_output: Expected return value of a tool (Tier 1 only — required when tier == 1).
        tool_calls: List of expected tool invocations (Tier 2/3 — required when tier in {2, 3}).
        routing: Routing decision (e.g., {'safety_critical': true, 'offers_diy_choice': false}).
        response: Response assertions (e.g., {'must_contain_one_of': ['warranty'], 'must_not_contain': ['DIY']}).
    """
    tool_output: dict[str, Any] | None = None
    tool_calls: list[Any] | None = None
    routing: dict[str, Any] | None = None
    response: dict[str, Any] | None = None


class EvalCase(BaseModel):
    """A single eval case (golden test) for the CVX agent.
    
    Attributes:
        id: Unique case identifier (kebab-case).
        description: Human-readable description of what the case tests.
        tier: Evaluation tier (1=unit, 2=conversation, 3=e2e).
        persona: Agent persona (e.g., 'fleet-driver').
        vehicle_id: Optional vehicle identifier for context.
        driver_id: Optional driver identifier for context.
        input: Input data (utterance, audio_path, or input_args).
        expected: Expected outputs and assertions.
        latency_budget_ms: Maximum allowed latency in milliseconds (tier-specific defaults if unset).
    """
    id: str
    description: str
    tier: Literal[1, 2, 3]
    persona: str
    vehicle_id: str | None = None
    driver_id: str | None = None
    input: Input
    expected: Expected
    latency_budget_ms: int | None = None

    @model_validator(mode="after")
    def validate_tier_fields(self) -> "EvalCase":
        if self.tier == 1:
            if self.expected.tool_output is None:
                raise ValueError("Tier 1 cases require expected.tool_output")
        elif self.tier in (2, 3):
            if self.expected.tool_calls is None or self.expected.response is None:
                raise ValueError(
                    "Tier 2/3 cases require both expected.tool_calls and expected.response"
                )
        return self
