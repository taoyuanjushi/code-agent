from __future__ import annotations

from typing import Any, Protocol

from openai import OpenAI

from .tools import TOOL_DEFINITIONS
from .types import AgentConfig


class ModelClient(Protocol):
    def create_initial_response(
        self,
        *,
        config: AgentConfig,
        instructions: str,
        input_text: str,
    ) -> Any:
        """Create the first model response for an agent run."""

    def create_tool_response(
        self,
        *,
        config: AgentConfig,
        previous_response_id: str,
        tool_outputs: list[dict[str, Any]],
    ) -> Any:
        """Continue an agent run after tool calls have executed."""


class OpenAIResponsesClient:
    def __init__(self, client: OpenAI | None = None) -> None:
        self._client = client or OpenAI()

    def create_initial_response(
        self,
        *,
        config: AgentConfig,
        instructions: str,
        input_text: str,
    ) -> Any:
        return self._client.responses.create(
            model=config.model,
            reasoning={
                "effort": config.reasoning_effort,
                "summary": "auto",
            },
            instructions=instructions,
            input=input_text,
            tools=TOOL_DEFINITIONS,
        )

    def create_tool_response(
        self,
        *,
        config: AgentConfig,
        previous_response_id: str,
        tool_outputs: list[dict[str, Any]],
    ) -> Any:
        return self._client.responses.create(
            model=config.model,
            reasoning={
                "effort": config.reasoning_effort,
                "summary": "auto",
            },
            previous_response_id=previous_response_id,
            input=tool_outputs,
            tools=TOOL_DEFINITIONS,
        )
