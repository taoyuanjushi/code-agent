import json
from dataclasses import dataclass
from typing import Any, Literal

from .context import (
    DEFAULT_MAX_INVENTORY_FILES,
    DEFAULT_MAX_TOTAL_SAMPLE_BYTES,
    collect_workspace_snapshot,
    format_snapshot,
)
from .instructions import discover_agent_instructions
from .model_client import ModelClient, OpenAIResponsesClient
from .prompts import build_system_prompt, build_user_prompt
from .tools import VerificationToolState, execute_tool
from .types import AgentConfig
from .verification import VerificationResult


@dataclass(frozen=True)
class AgentRunReport:
    answer: str
    verifications: tuple[VerificationResult, ...]
    final_status: Literal["passed", "failed", "not_run"]


def run_agent(
    task: str,
    config: AgentConfig,
    model_client: ModelClient | None = None,
) -> str:
    return run_agent_with_report(task, config, model_client=model_client).answer


def run_agent_with_report(
    task: str,
    config: AgentConfig,
    model_client: ModelClient | None = None,
) -> AgentRunReport:
    client = model_client or OpenAIResponsesClient()
    verification_state = VerificationToolState(
        task=task,
        max_fix_attempts=config.max_fix_attempts,
    )
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
            answer = _get_output_text(response)
            final_status = _verification_final_status(verification_state)
            if final_status == "failed":
                answer = (
                    f"{answer}\n\n{_verification_failure_note(verification_state)}"
                ).strip()
            return AgentRunReport(
                answer=answer,
                verifications=tuple(verification_state.verification_history),
                final_status=final_status,
            )

        tool_outputs: list[dict[str, Any]] = []
        for call in tool_calls:
            print(f"\ntool: {call['name']}")
            result = execute_tool(
                config,
                call["name"],
                call["arguments"],
                state=verification_state,
            )
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
                            "data": result.data,
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


def _verification_final_status(
    state: VerificationToolState,
) -> Literal["passed", "failed", "not_run"]:
    if not state.verification_history:
        return "not_run"

    latest_by_command = _latest_verification_results(state)
    if not all(result.status == "passed" for result in latest_by_command.values()):
        return "failed"
    if not all(
        state.passed_generations.get(command_id) == state.edit_generation
        for command_id in latest_by_command
    ):
        return "failed"
    return "passed"


def _verification_failure_note(state: VerificationToolState) -> str:
    latest_by_command = _latest_verification_results(state)
    messages: list[str] = []

    non_passing = [
        f"{command_id} ({result.status})"
        for command_id, result in latest_by_command.items()
        if result.status != "passed"
    ]
    if non_passing:
        messages.append(f"Verification did not pass: {', '.join(non_passing)}.")

    stale = [
        command_id
        for command_id, result in latest_by_command.items()
        if result.status == "passed"
        and state.passed_generations.get(command_id) != state.edit_generation
    ]
    if stale:
        messages.append(
            "Verification results are stale after the latest edit and were not "
            f"rerun: {', '.join(stale)}."
        )

    return " ".join(messages) or "Verification is incomplete."


def _latest_verification_results(
    state: VerificationToolState,
) -> dict[str, VerificationResult]:
    latest_by_command: dict[str, VerificationResult] = {}
    for result in state.verification_history:
        latest_by_command[result.command_id] = result
    return latest_by_command


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
