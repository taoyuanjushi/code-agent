from __future__ import annotations

from typing import Any, Protocol

from openai import OpenAI

from .sessions.models import ModelFunctionCall, NormalizedModelResponse
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


def normalize_model_response(response: Any) -> NormalizedModelResponse:
    """Convert an SDK or mapping response into the durable model domain shape."""

    response_id = _get_attr_or_key(response, "id")
    if not isinstance(response_id, str) or not response_id:
        raise RuntimeError("Model response did not include a response id.")

    calls: list[ModelFunctionCall] = []
    reasoning_summaries: list[str] = []
    message_texts: list[str] = []
    for item in _get_output_items(response):
        item_type = _get_attr_or_key(item, "type")
        if item_type == "function_call":
            name = _get_attr_or_key(item, "name")
            arguments = _get_attr_or_key(item, "arguments")
            call_id = _get_attr_or_key(item, "call_id")
            if (
                isinstance(name, str)
                and isinstance(arguments, str)
                and isinstance(call_id, str)
            ):
                calls.append(
                    ModelFunctionCall(
                        call_id=call_id,
                        name=name,
                        arguments=arguments,
                    )
                )
        elif item_type == "reasoning":
            summary = _read_reasoning_summary(item)
            if summary:
                reasoning_summaries.append(summary)
        elif item_type == "message":
            text = _read_message_text(item)
            if text:
                message_texts.append(text)

    output_text = _get_attr_or_key(response, "output_text")
    text = output_text if isinstance(output_text, str) else "\n".join(message_texts)
    return NormalizedModelResponse(
        response_id=response_id,
        text=text,
        reasoning_summary="\n".join(reasoning_summaries),
        function_calls=tuple(calls),
    )


def _get_output_items(response: Any) -> list[Any]:
    output = _get_attr_or_key(response, "output")
    return output if isinstance(output, list) else []


def _read_reasoning_summary(item: Any) -> str:
    summary_entries = _get_attr_or_key(item, "summary") or []
    if not isinstance(summary_entries, list):
        return ""
    texts: list[str] = []
    for entry in summary_entries:
        text = _get_attr_or_key(entry, "text")
        if isinstance(text, str):
            texts.append(text)
    return "\n".join(texts).strip()


def _read_message_text(item: Any) -> str:
    content = _get_attr_or_key(item, "content")
    if not isinstance(content, list):
        return ""
    texts: list[str] = []
    for content_item in content:
        text = _get_attr_or_key(content_item, "text")
        if isinstance(text, str):
            texts.append(text)
    return "\n".join(texts)


def _get_attr_or_key(value: Any, key: str) -> Any:
    if isinstance(value, dict):
        return value.get(key)
    return getattr(value, key, None)
