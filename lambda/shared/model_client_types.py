"""shared.model_client_types — unified request/response/streaming types.

These types are the public surface of shared.model_client. They are
deliberately Converse-flavored on input (Converse-shaped content blocks
in messages, top-level system, toolSpec-shaped tool definitions) because
the existing codebase already speaks this dialect; the Responses backend
translates inbound to OpenAI shape and outbound back to this shape.

Per ADR-035 §5, streaming is a peer of non-streaming: every backend
emits the same StreamEvent hierarchy regardless of whether the wire
format was Converse EventStream or OpenAI SSE.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterator, Optional


class StopReason(str, Enum):
    END_TURN = "end_turn"
    TOOL_USE = "tool_use"
    MAX_TOKENS = "max_tokens"
    STOP_SEQUENCE = "stop_sequence"


@dataclass
class ToolSpec:
    """Tool definition. input_schema is JSON Schema (the same dict for both backends)."""

    name: str
    description: str
    input_schema: dict


@dataclass
class ToolCall:
    """A tool invocation extracted from an assistant response."""

    tool_use_id: str
    name: str
    input: dict


@dataclass
class Usage:
    input_tokens: int
    output_tokens: int


@dataclass
class Request:
    """Unified completion request. Converse-flavored content blocks in messages."""

    model_id: str
    messages: list[dict]
    system: Optional[str | list[str]] = None
    max_tokens: int = 4096
    temperature: float = 0.0
    top_p: Optional[float] = None
    stop_sequences: Optional[list[str]] = None
    tools: Optional[list[ToolSpec]] = None


@dataclass
class Response:
    """Unified completion response. content_blocks is Converse-shaped."""

    text: str
    content_blocks: list[dict]
    usage: Usage
    stop_reason: StopReason
    tool_calls: Optional[list[ToolCall]] = None
    raw: Any = None


# === streaming event hierarchy (ADR-035 §5) ===


@dataclass
class StreamEvent:
    """Base class for unified streaming events. Use isinstance() dispatch."""


@dataclass
class TextDelta(StreamEvent):
    text: str


@dataclass
class ToolUseStart(StreamEvent):
    tool_use_id: str
    name: str


@dataclass
class ToolUseArgumentsDelta(StreamEvent):
    tool_use_id: str
    arguments_partial: str


@dataclass
class ToolUseStop(StreamEvent):
    tool_use_id: str
    arguments: dict


@dataclass
class Stop(StreamEvent):
    stop_reason: StopReason


@dataclass
class Metadata(StreamEvent):
    usage: Usage


# === errors ===


class ModelClientError(Exception):
    """Base for all shared.model_client errors."""


class UnsupportedModelError(ModelClientError):
    """Model ID prefix does not match any registered backend."""


class BackendConfigError(ModelClientError):
    """A backend resolved but is misconfigured (e.g. missing region)."""


class TranslationError(ModelClientError):
    """A request or response shape cannot be translated between APIs."""
