import json
from typing import Any

from .context import (
    DEFAULT_MAX_INVENTORY_FILES,
    DEFAULT_MAX_TOTAL_SAMPLE_BYTES,
    collect_workspace_snapshot,
    format_snapshot,
)
from .instructions import discover_agent_instructions
from .model_client import ModelClient, OpenAIResponsesClient
from .prompts import build_system_prompt, build_user_prompt
from .tools import execute_tool
from .types import AgentConfig


def run_agent(
    task: str,
    config: AgentConfig,
    model_client: ModelClient | None = None,
) -> str:
    client = model_client or OpenAIResponsesClient()
    repository_instructions = discover_agent_instructions(config.workspace)
    snapshot = collect_workspace_snapshot(
        config.workspace,
        task,
        max_inventory_files=DEFAULT_MAX_INVENTORY_FILES,
        max_sample_files=config.context_max_files,
        max_bytes_per_file=config.context_max_bytes_per_file,
        max_total_sample_bytes=DEFAULT_MAX_TOTAL_SAMPLE_BYTES,
    )
    workspace_context = format_snapshot(snapshot)

    response = client.create_initial_response(
        config=config,
        instructions=build_system_prompt(config, repository_instructions),
        input_text=build_user_prompt(task, workspace_context),
    )

    for _turn in range(config.max_turns):
        _print_response_messages(response)
        tool_calls = _find_function_calls(response)

        if not tool_calls:
            return _get_output_text(response)

        tool_outputs: list[dict[str, Any]] = []
        for call in tool_calls:
            print(f"\ntool: {call['name']}")
            result = execute_tool(config, call["name"], call["arguments"])
            print("ok" if result.ok else "failed")
            if result.output:
                print(_truncate_for_console(result.output))

            tool_outputs.append(
                {
                    "type": "function_call_output",
                    "call_id": call["call_id"],
                    "output": json.dumps(
                        {
                            "ok": result.ok,
                            "output": result.output,
                        },
                        ensure_ascii=False,
                    ),
                }
            )

        response = client.create_tool_response(
            config=config,
            previous_response_id=_get_response_id(response),
            tool_outputs=tool_outputs,
        )

    raise RuntimeError(f"Agent stopped after reaching max turn limit ({config.max_turns}).")


def _get_response_id(response: Any) -> str:
    response_id = _get_attr_or_key(response, "id")
    if not isinstance(response_id, str):
        raise RuntimeError("Model response did not include a response id.")
    return response_id


def _find_function_calls(response: Any) -> list[dict[str, str]]:
    calls: list[dict[str, str]] = []
    for item in _get_output_items(response):
        if (
            _get_attr_or_key(item, "type") == "function_call"
            and isinstance(_get_attr_or_key(item, "name"), str)
            and isinstance(_get_attr_or_key(item, "arguments"), str)
            and isinstance(_get_attr_or_key(item, "call_id"), str)
        ):
            calls.append(
                {
                    "name": _get_attr_or_key(item, "name"),
                    "arguments": _get_attr_or_key(item, "arguments"),
                    "call_id": _get_attr_or_key(item, "call_id"),
                }
            )
    return calls


def _print_response_messages(response: Any) -> None:
    for item in _get_output_items(response):
        item_type = _get_attr_or_key(item, "type")

        if item_type == "reasoning":
            summary_entries = _get_attr_or_key(item, "summary") or []
            summary = "\n".join(
                entry.text if hasattr(entry, "text") else entry.get("text", "")
                for entry in summary_entries
                if hasattr(entry, "text") or isinstance(entry, dict)
            ).strip()
            if summary:
                print(f"\nreasoning summary:\n{summary}")

        if item_type == "message":
            text = _read_message_text(item)
            if text:
                print(f"\n{text}")


def _get_output_text(response: Any) -> str:
    output_text = _get_attr_or_key(response, "output_text")
    if isinstance(output_text, str):
        return output_text

    return "\n".join(
        text
        for text in (_read_message_text(item) for item in _get_output_items(response))
        if text
    )


def _get_output_items(response: Any) -> list[Any]:
    output = getattr(response, "output", None)
    if isinstance(output, list):
        return output
    if isinstance(response, dict) and isinstance(response.get("output"), list):
        return response["output"]
    return []


def _read_message_text(item: Any) -> str:
    content = _get_attr_or_key(item, "content")
    if not isinstance(content, list):
        return ""

    texts = []
    for content_item in content:
        text = _get_attr_or_key(content_item, "text")
        if isinstance(text, str):
            texts.append(text)
    return "\n".join(texts)


def _get_attr_or_key(value: Any, key: str) -> Any:
    if isinstance(value, dict):
        return value.get(key)
    return getattr(value, key, None)


def _truncate_for_console(value: str) -> str:
    limit = 2000
    if len(value) <= limit:
        return value
    return f"{value[:limit]}\n[console output truncated]"
