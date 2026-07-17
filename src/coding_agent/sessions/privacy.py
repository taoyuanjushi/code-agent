from __future__ import annotations

import json
import math
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from ..types import AgentConfig
from ..security.models import SECURITY_POLICY_VERSION
from .codec import artifact_ref_to_dict
from .models import ArtifactRef, JsonValue

INLINE_PAYLOAD_MAX_BYTES = 64 * 1024
ARTIFACT_MAX_BYTES = 4 * 1024 * 1024
REDACTION_MARKER = "[REDACTED]"
_EXCEPTION_MESSAGE_MAX_BYTES = 512

ArtifactWriter = Callable[[bytes, str, str | None], ArtifactRef]


@dataclass(frozen=True)
class SessionPrivacyPolicy:
    """Normalize session data without persisting credentials or large values inline."""

    inline_max_bytes: int = INLINE_PAYLOAD_MAX_BYTES
    artifact_max_bytes: int = ARTIFACT_MAX_BYTES

    def __post_init__(self) -> None:
        _validate_positive_int(self.inline_max_bytes, "inline_max_bytes")
        _validate_positive_int(self.artifact_max_bytes, "artifact_max_bytes")
        if self.inline_max_bytes >= self.artifact_max_bytes:
            raise ValueError(
                "inline_max_bytes must be smaller than artifact_max_bytes."
            )

    def sanitize_config(self, config: AgentConfig) -> dict[str, object]:
        """Return only the AgentConfig fields required to resume a session."""

        if not isinstance(config, AgentConfig):
            raise TypeError("config must be an AgentConfig.")

        secrets = _discover_sensitive_environment_values()
        return {
            "workspace": _redact_text(config.workspace, secrets),
            "model": _redact_text(config.model, secrets),
            "reasoning_effort": config.reasoning_effort,
            "max_turns": config.max_turns,
            "permission_mode": config.permission_mode,
            "auto_approve_commands": config.auto_approve_commands,
            "auto_approve_edits": config.auto_approve_edits,
            "context_max_files": config.context_max_files,
            "context_max_bytes_per_file": config.context_max_bytes_per_file,
            "max_fix_attempts": config.max_fix_attempts,
            "sandbox_mode": config.sandbox_mode,
            "sandbox_image": _redact_text(config.sandbox_image, secrets),
            "sandbox_image_digest": config.sandbox_image_digest,
            "full_auto": config.full_auto,
            "security_policy_version": SECURITY_POLICY_VERSION,
        }

    def sanitize_artifact_content(self, content: bytes) -> bytes:
        """Redact known environment secret values from artifact bytes."""

        if not isinstance(content, bytes):
            raise TypeError("artifact content must be bytes.")
        secrets = _discover_sensitive_environment_values()
        return _redact_bytes(content, secrets)

    def sanitize_payload(
        self,
        value: object,
        *,
        artifact_writer: ArtifactWriter | None = None,
    ) -> JsonValue:
        """Redact, normalize, and externalize one JSON-compatible payload value."""

        if artifact_writer is not None and not callable(artifact_writer):
            raise TypeError("artifact_writer must be callable or null.")

        secrets = _discover_sensitive_environment_values()
        sanitized = self._sanitize_value(
            value,
            path=(),
            active_ids=set(),
            secrets=secrets,
            artifact_writer=artifact_writer,
        )
        encoded = _canonical_json_bytes(sanitized)
        if len(encoded) <= self.inline_max_bytes:
            return sanitized

        return self._externalize(
            encoded,
            original_byte_count=len(encoded),
            media_type="application/json",
            encoding="utf-8",
            content_kind="payload",
            artifact_writer=artifact_writer,
        )

    def _sanitize_value(
        self,
        value: object,
        *,
        path: tuple[str, ...],
        active_ids: set[int],
        secrets: tuple[str, ...],
        artifact_writer: ArtifactWriter | None,
    ) -> JsonValue:
        if value is None or isinstance(value, bool):
            return value
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            if not math.isfinite(value):
                raise ValueError("Session payload numbers must be finite.")
            return value
        if isinstance(value, str):
            return self._sanitize_text(
                value,
                path=path,
                secrets=secrets,
                artifact_writer=artifact_writer,
            )
        if isinstance(value, (bytes, bytearray, memoryview)):
            raw = bytes(value)
            redacted = _redact_bytes(raw, secrets)
            return self._externalize(
                redacted,
                original_byte_count=len(raw),
                media_type="application/octet-stream",
                encoding=None,
                content_kind="binary data",
                artifact_writer=artifact_writer,
            )
        if isinstance(value, AgentConfig):
            return self.sanitize_config(value)
        if isinstance(value, BaseException):
            return self._exception_summary(value, secrets)
        if isinstance(value, Path):
            return self._sanitize_text(
                str(value),
                path=path,
                secrets=secrets,
                artifact_writer=artifact_writer,
            )
        if value is os.environ:
            return {
                "summary_type": "process_context",
                "stored": False,
                "redacted": True,
            }
        if _is_header_or_auth_object(value):
            return {
                "summary_type": "authentication_or_header_object",
                "redacted": True,
                "object_type": _safe_type_name(value),
            }
        if isinstance(value, Mapping):
            return self._sanitize_mapping(
                value,
                path=path,
                active_ids=active_ids,
                secrets=secrets,
                artifact_writer=artifact_writer,
            )
        if isinstance(value, (list, tuple)):
            return self._sanitize_sequence(
                value,
                path=path,
                active_ids=active_ids,
                secrets=secrets,
                artifact_writer=artifact_writer,
            )
        return {
            "summary_type": "unsupported_object",
            "stored": False,
            "object_type": _safe_type_name(value),
        }

    def _sanitize_mapping(
        self,
        value: Mapping[object, object],
        *,
        path: tuple[str, ...],
        active_ids: set[int],
        secrets: tuple[str, ...],
        artifact_writer: ArtifactWriter | None,
    ) -> JsonValue:
        identity = id(value)
        if identity in active_ids:
            return {
                "summary_type": "cyclic_reference",
                "stored": False,
                "object_type": _safe_type_name(value),
            }

        active_ids.add(identity)
        result: dict[str, JsonValue] = {}
        omitted = {
            "secret_field_count": 0,
            "header_object_count": 0,
            "process_context_count": 0,
            "non_string_key_count": 0,
        }
        try:
            for key, item in value.items():
                if not isinstance(key, str):
                    omitted["non_string_key_count"] += 1
                    continue

                normalized_key = _normalize_text(key)
                if _redact_text(normalized_key, secrets) != normalized_key:
                    omitted["secret_field_count"] += 1
                    continue
                if _is_process_context_field(normalized_key):
                    omitted["process_context_count"] += 1
                    continue
                if _is_header_field(normalized_key):
                    omitted["header_object_count"] += 1
                    continue
                if _is_sensitive_field(normalized_key):
                    omitted["secret_field_count"] += 1
                    continue

                result[normalized_key] = self._sanitize_value(
                    item,
                    path=(*path, normalized_key),
                    active_ids=active_ids,
                    secrets=secrets,
                    artifact_writer=artifact_writer,
                )
        finally:
            active_ids.remove(identity)

        counts = {key: count for key, count in omitted.items() if count}
        if counts:
            result[_available_privacy_key(result)] = {
                "redacted": True,
                **counts,
            }
        return result

    def _sanitize_sequence(
        self,
        value: list[object] | tuple[object, ...],
        *,
        path: tuple[str, ...],
        active_ids: set[int],
        secrets: tuple[str, ...],
        artifact_writer: ArtifactWriter | None,
    ) -> JsonValue:
        identity = id(value)
        if identity in active_ids:
            return (
                {
                    "summary_type": "cyclic_reference",
                    "stored": False,
                    "object_type": _safe_type_name(value),
                },
            )

        active_ids.add(identity)
        try:
            return tuple(
                self._sanitize_value(
                    item,
                    path=(*path, str(index)),
                    active_ids=active_ids,
                    secrets=secrets,
                    artifact_writer=artifact_writer,
                )
                for index, item in enumerate(value)
            )
        finally:
            active_ids.remove(identity)

    def _sanitize_text(
        self,
        value: str,
        *,
        path: tuple[str, ...],
        secrets: tuple[str, ...],
        artifact_writer: ArtifactWriter | None,
    ) -> JsonValue:
        normalized = _normalize_text(value)
        original_byte_count = len(normalized.encode("utf-8"))
        redacted = _redact_text(normalized, secrets)
        if original_byte_count <= self.inline_max_bytes:
            return redacted

        media_type, content_kind = _text_artifact_metadata(path)
        return self._externalize(
            redacted.encode("utf-8"),
            original_byte_count=original_byte_count,
            media_type=media_type,
            encoding="utf-8",
            content_kind=content_kind,
            artifact_writer=artifact_writer,
        )

    def _externalize(
        self,
        content: bytes,
        *,
        original_byte_count: int,
        media_type: str,
        encoding: str | None,
        content_kind: str,
        artifact_writer: ArtifactWriter | None,
    ) -> JsonValue:
        if (
            original_byte_count > self.artifact_max_bytes
            or len(content) > self.artifact_max_bytes
        ):
            return {
                "stored": False,
                "original_byte_count": original_byte_count,
                "reason": "exceeds_artifact_max_bytes",
                "summary": (
                    f"{content_kind} omitted because it exceeds "
                    "the artifact limit"
                ),
            }
        if artifact_writer is None:
            return {
                "stored": False,
                "original_byte_count": original_byte_count,
                "reason": "artifact_writer_unavailable",
                "summary": (
                    f"{content_kind} omitted because artifact storage "
                    "is unavailable"
                ),
            }

        ref = artifact_writer(content, media_type, encoding)
        return {
            "stored": True,
            "original_byte_count": original_byte_count,
            "summary": f"{content_kind} stored as a content-addressed artifact",
            "artifact": artifact_ref_to_dict(ref),
        }

    def _exception_summary(
        self,
        error: BaseException,
        secrets: tuple[str, ...],
    ) -> JsonValue:
        try:
            message = str(error)
        except BaseException:
            message = "exception message unavailable"
        normalized = _redact_text(_normalize_text(message), secrets)
        message, truncated = _truncate_utf8(
            normalized,
            _EXCEPTION_MESSAGE_MAX_BYTES,
        )
        return {
            "summary_type": "exception",
            "exception_type": _safe_type_name(error),
            "message": message,
            "message_truncated": truncated,
        }


def _discover_sensitive_environment_values() -> tuple[str, ...]:
    values: set[str] = set()
    for name, value in os.environ.items():
        if value and _is_sensitive_environment_name(name):
            values.add(_normalize_text(value))
    return tuple(sorted(values, key=lambda item: (-len(item), item)))


def _is_sensitive_environment_name(name: str) -> bool:
    upper = name.upper()
    return upper == "OPENAI_API_KEY" or upper.endswith(
        ("_TOKEN", "_SECRET", "_PASSWORD")
    )


def _is_process_context_field(name: str) -> bool:
    return _normalized_field_name(name) in {"ENV", "ENVIRON", "ENVIRONMENT"}


def _is_header_field(name: str) -> bool:
    normalized = _normalized_field_name(name)
    return normalized in {
        "HEADERS",
        "HTTP_HEADERS",
        "REQUEST_HEADERS",
        "RESPONSE_HEADERS",
    } or normalized.endswith("_HEADERS")


def _is_sensitive_field(name: str) -> bool:
    normalized = _normalized_field_name(name)
    if normalized == "OPENAI_API_KEY":
        return True
    if normalized.endswith(("_TOKEN", "_SECRET", "_PASSWORD", "_API_KEY")):
        return True
    return normalized in {
        "API_KEY",
        "AUTH",
        "AUTHENTICATION",
        "AUTHORIZATION",
        "COOKIE",
        "CREDENTIAL",
        "CREDENTIALS",
        "PASSWORD",
        "SECRET",
        "SET_COOKIE",
        "TOKEN",
    }


def _is_header_or_auth_object(value: object) -> bool:
    name = type(value).__name__.lower()
    return any(
        marker in name
        for marker in ("auth", "credential", "header", "password", "secret", "token")
    )


def _text_artifact_metadata(path: tuple[str, ...]) -> tuple[str, str]:
    field = path[-1].lower() if path else ""
    if "diff" in field or "patch" in field:
        return "text/x-diff", "diff"
    if "traceback" in field or "stack_trace" in field:
        return "text/x-python-traceback", "traceback"
    if "prompt" in field or field in {"input", "instructions"}:
        return "text/plain", "prompt"
    if "output" in field or "result" in field:
        return "text/plain", "tool output"
    if "argument" in field:
        return "application/json", "tool arguments"
    return "text/plain", "text content"


def _redact_text(value: str, secrets: tuple[str, ...]) -> str:
    redacted = value
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, REDACTION_MARKER)
    return redacted


def _redact_bytes(value: bytes, secrets: tuple[str, ...]) -> bytes:
    redacted = value
    marker = REDACTION_MARKER.encode("utf-8")
    for secret in secrets:
        encoded = secret.encode("utf-8")
        if encoded:
            redacted = redacted.replace(encoded, marker)
    return redacted


def _normalize_text(value: str) -> str:
    return value.encode("utf-8", errors="replace").decode("utf-8")


def _normalized_field_name(value: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", value.upper()).strip("_")


def _available_privacy_key(value: Mapping[str, object]) -> str:
    candidate = "_privacy"
    suffix = 2
    while candidate in value:
        candidate = f"_privacy_{suffix}"
        suffix += 1
    return candidate


def _safe_type_name(value: object) -> str:
    name = type(value).__name__
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name)[:128] or "unknown"


def _truncate_utf8(value: str, max_bytes: int) -> tuple[str, bool]:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value, False
    return encoded[:max_bytes].decode("utf-8", errors="ignore"), True


def _canonical_json_bytes(value: JsonValue) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _validate_positive_int(value: object, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer.")
