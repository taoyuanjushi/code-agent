from __future__ import annotations

import subprocess
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Literal, TypeAlias, cast

from .tool_policy import get_tool_policy
from .types import AgentConfig

ApprovalOutcome = Literal["approved", "denied"]
ApprovalSource = Literal["interactive", "auto_policy", "resume_recovery"]
ApprovalDetailScalar: TypeAlias = str | int | float | bool | None
ApprovalDetail: TypeAlias = (
    ApprovalDetailScalar
    | tuple["ApprovalDetail", ...]
    | Mapping[str, "ApprovalDetail"]
)

APPROVAL_OUTCOMES = frozenset({"approved", "denied"})
APPROVAL_SOURCES = frozenset(
    {"interactive", "auto_policy", "resume_recovery"}
)


@dataclass(frozen=True)
class ApprovalRequest:
    call_id: str
    action: str
    summary: str
    arguments_sha256: str
    details: Mapping[str, ApprovalDetail]

    def __post_init__(self) -> None:
        _validate_non_empty_string(self.call_id, "call_id")
        _validate_non_empty_string(self.action, "approval action")
        _validate_non_empty_string(self.summary, "approval summary")
        _validate_sha256(self.arguments_sha256, "arguments_sha256")
        object.__setattr__(self, "details", _freeze_details(self.details))


@dataclass(frozen=True)
class ApprovalDecision:
    approval_id: str
    call_id: str
    action: str
    summary: str
    outcome: ApprovalOutcome
    source: ApprovalSource
    decided_at: str
    arguments_sha256: str

    def __post_init__(self) -> None:
        _validate_non_empty_string(self.approval_id, "approval_id")
        _validate_non_empty_string(self.call_id, "call_id")
        _validate_non_empty_string(self.action, "approval action")
        _validate_non_empty_string(self.summary, "approval summary")
        if self.outcome not in APPROVAL_OUTCOMES:
            raise ValueError(f"Unsupported approval outcome: {self.outcome}")
        if self.source not in APPROVAL_SOURCES:
            raise ValueError(f"Unsupported approval source: {self.source}")
        _validate_utc_timestamp(self.decided_at, "decided_at")
        _validate_sha256(self.arguments_sha256, "arguments_sha256")


ApprovalHandler = Callable[[ApprovalRequest], ApprovalDecision]
ApprovalInputReader: TypeAlias = Callable[[str], str]



def create_approval_decision(
    request: ApprovalRequest,
    *,
    approved: bool,
    source: ApprovalSource,
    summary: str | None = None,
) -> ApprovalDecision:
    return ApprovalDecision(
        approval_id=f"approval-{uuid.uuid4().hex}",
        call_id=request.call_id,
        action=request.action,
        summary=summary or request.summary,
        outcome="approved" if approved else "denied",
        source=source,
        decided_at=_utc_now(),
        arguments_sha256=request.arguments_sha256,
    )



def validate_approval_decision(
    request: ApprovalRequest,
    decision: ApprovalDecision,
) -> None:
    if not isinstance(decision, ApprovalDecision):
        raise TypeError("approval handler must return ApprovalDecision.")
    mismatches: list[str] = []
    if decision.call_id != request.call_id:
        mismatches.append("call_id")
    if decision.action != request.action:
        mismatches.append("action")
    if decision.arguments_sha256 != request.arguments_sha256:
        mismatches.append("arguments_sha256")
    if mismatches:
        raise ValueError(
            "approval decision does not match its request: "
            + ", ".join(mismatches)
        )



def build_resume_recovery_approval_handler(
    *,
    request_writer: Callable[[str], None] = print,
    input_reader: ApprovalInputReader | None = None,
) -> ApprovalHandler:
    """Create an interactive recovery handler that never honors auto-approval."""
    if not callable(request_writer):
        raise TypeError("request_writer must be callable.")
    if input_reader is not None and not callable(input_reader):
        raise TypeError("input_reader must be callable or null.")

    def handle(request: ApprovalRequest) -> ApprovalDecision:
        policy = get_tool_policy(request.action)
        if not policy.approval_required:
            raise ValueError(
                f"Tool {request.action!r} does not require approval."
            )

        try:
            rendered = render_approval_request(request)
        except ValueError:
            arguments = request.details.get("arguments", "unavailable")
            rendered = (
                f"Retry interrupted {request.action}?\n{request.summary}\n"
                f"arguments: {arguments}"
            )
        request_writer(rendered)
        approved = _read_approval_input(
            "Retry interrupted tool call? [y/N] ",
            input_reader,
        ).strip().lower() in {"y", "yes"}
        return create_approval_decision(
            request,
            approved=approved,
            source="resume_recovery",
            summary=(
                request.summary
                if approved
                else f"Denied recovery retry: {request.summary}"
            ),
        )

    return handle


def validate_resume_recovery_decision(
    request: ApprovalRequest,
    decision: ApprovalDecision,
) -> None:
    """Validate identity binding and require the recovery-specific source."""

    validate_approval_decision(request, decision)
    if decision.source != "resume_recovery":
        raise ValueError(
            "Interrupted tool retries require source='resume_recovery'; "
            "interactive and auto-policy decisions cannot be reused."
        )


def build_default_approval_handler(
    config: AgentConfig,
    *,
    request_writer: Callable[[str], None] = print,
    input_reader: ApprovalInputReader | None = None,
) -> ApprovalHandler:
    """Create the only production approval handler that reads stdin."""
    if not callable(request_writer):
        raise TypeError("request_writer must be callable.")
    if input_reader is not None and not callable(input_reader):
        raise TypeError("input_reader must be callable or null.")

    def handle(request: ApprovalRequest) -> ApprovalDecision:
        policy = get_tool_policy(request.action)
        if not policy.approval_required:
            raise ValueError(
                f"Tool {request.action!r} does not require approval."
            )

        request_writer(render_approval_request(request))
        auto_approved = (
            policy.approval_group == "edits" and config.auto_approve_edits
        ) or (
            policy.approval_group == "commands" and config.auto_approve_commands
        )
        if auto_approved:
            return create_approval_decision(
                request,
                approved=True,
                source="auto_policy",
            )

        prompt = (
            "Apply patch? [y/N] "
            if policy.approval_group == "edits"
            else "Run command? [y/N] "
        )
        approved = _read_approval_input(prompt, input_reader).strip().lower() in {
            "y",
            "yes",
        }
        return create_approval_decision(
            request,
            approved=approved,
            source="interactive",
            summary=(request.summary if approved else f"Denied: {request.summary}"),
        )

    return handle



def _read_approval_input(
    prompt: str,
    input_reader: ApprovalInputReader | None,
) -> str:
    if input_reader is None:
        return input(prompt)
    response = input_reader(prompt)
    if not isinstance(response, str):
        raise TypeError("input_reader must return a string.")
    return response


def render_approval_request(request: ApprovalRequest) -> str:
    details = request.details
    if request.action == "apply_patch":
        workspace = _detail_string(details, "workspace")
        summary = _detail_string(details, "change_summary")
        patch = _detail_string(details, "patch")
        return (
            f"Apply patch in {workspace}?\n\n"
            f"Change summary:\n{summary}\n\n"
            f"Unified diff:\n{patch}"
        )

    if request.action == "run_verification":
        argv = _detail_string_tuple(details, "argv")
        cwd = _detail_string(details, "cwd")
        timeout_ms = details.get("timeout_ms")
        rendered = subprocess.list2cmdline(list(argv))
        return (
            f"Run verification in {cwd}?\n{rendered}\n"
            f"timeout_ms: {timeout_ms}\n"
            f"{_render_execution_security(details)}"
        )

    if request.action == "run_command":
        argv = _detail_string_tuple(details, "argv")
        cwd = _detail_string(details, "cwd")
        timeout_ms = details.get("timeout_ms")
        rendered = subprocess.list2cmdline(list(argv))
        return (
            f"Run command in {cwd}?\n{rendered}\n"
            f"timeout_ms: {timeout_ms}\n"
            f"{_render_execution_security(details)}"
        )

    return f"Approve {request.action}?\n{request.summary}"


def _render_execution_security(
    details: Mapping[str, ApprovalDetail],
) -> str:
    backend = details.get("backend", "host")
    sandboxed = details.get("sandboxed", False)
    network_mode = details.get("network_mode", "host")
    rule_id = details.get("rule_id", "unavailable")
    reasons = details.get("reasons", ())
    if isinstance(reasons, tuple):
        rendered_reasons = "; ".join(
            reason for reason in reasons if isinstance(reason, str)
        ) or "none"
    else:
        rendered_reasons = "unavailable"
    image_reference = details.get("image_reference")
    image_digest = details.get("image_digest")
    lines = [
        f"backend: {backend}",
        f"sandboxed: {str(sandboxed).lower()}",
        f"network_mode: {network_mode}",
        f"policy_rule: {rule_id}",
        f"policy_reasons: {rendered_reasons}",
    ]
    if image_reference is not None:
        lines.append(f"image: {image_reference}")
    if image_digest is not None:
        lines.append(f"image_digest: {image_digest}")
    return "\n".join(lines)


def _detail_string(
    details: Mapping[str, ApprovalDetail],
    key: str,
) -> str:
    value = details.get(key)
    if not isinstance(value, str):
        raise ValueError(f"approval detail {key!r} must be a string.")
    return value



def _detail_string_tuple(
    details: Mapping[str, ApprovalDetail],
    key: str,
) -> tuple[str, ...]:
    value = details.get(key)
    if not isinstance(value, tuple) or not all(
        isinstance(item, str) for item in value
    ):
        raise ValueError(
            f"approval detail {key!r} must be a sequence of strings."
        )
    return cast(tuple[str, ...], value)



def _freeze_details(value: object) -> Mapping[str, ApprovalDetail]:
    if not isinstance(value, Mapping):
        raise TypeError("approval details must be a mapping.")
    return MappingProxyType(
        {
            str(key): _freeze_detail(item)
            for key, item in value.items()
        }
    )



def _freeze_detail(value: object) -> ApprovalDetail:
    if value is None or isinstance(value, (str, bool, int, float)):
        return cast(ApprovalDetailScalar, value)
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze_detail(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_detail(item) for item in value)
    raise TypeError(
        f"approval details are not JSON-compatible: {type(value).__name__}."
    )



def _validate_non_empty_string(value: object, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string.")



def _validate_sha256(value: object, label: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{label} must be a lowercase SHA-256 hex digest.")



def _validate_utc_timestamp(value: object, label: str) -> None:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError(f"{label} must be an ISO-8601 UTC timestamp ending in Z.")
    try:
        parsed = datetime.fromisoformat(f"{value[:-1]}+00:00")
    except ValueError as exc:
        raise ValueError(f"{label} must be a valid ISO-8601 UTC timestamp.") from exc
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        raise ValueError(f"{label} must be UTC.")



def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )
