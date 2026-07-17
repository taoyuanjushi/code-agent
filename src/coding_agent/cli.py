from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import replace
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv

from .agent import AgentRunReport, resume_agent_with_report, run_agent_with_report
from .config import load_config
from .security.docker_backend import DockerSandboxBackend
from .sessions.query import (
    build_session_list_payload,
    build_session_replay_payload,
    resolve_session_selector,
)
from .sessions.replay import build_approval_query_payload
from .sessions.store import SessionStore
from .types import AgentConfig

CliMode = Literal["new", "resume", "replay", "list", "approvals"]


class CliUsageError(ValueError):
    """Raised when parsed CLI options do not describe one valid mode."""


_NEW_TASK_OPTIONS = (
    ("model", "--model"),
    ("reasoning_effort", "--reasoning-effort"),
    ("max_turns", "--max-turns"),
    ("write", "--write"),
    ("auto_approve_edits", "--auto-approve-edits"),
    ("auto_approve_commands", "--auto-approve-commands"),
    ("sandbox", "--sandbox"),
    ("sandbox_image", "--sandbox-image"),
    ("full_auto", "--full-auto"),
    ("max_fix_attempts", "--max-fix-attempts"),
    ("context_max_files", "--context-max-files"),
    ("context_max_bytes_per_file", "--context-max-bytes-per-file"),
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="coding-agent",
        description="A local AI coding agent CLI inspired by Codex.",
    )
    parser.add_argument("task", nargs="*", help="coding task to perform")
    parser.add_argument("-w", "--workspace", help="workspace path")

    session_modes = parser.add_mutually_exclusive_group()
    session_modes.add_argument(
        "--resume",
        metavar="SESSION_ID",
        help="resume a durable session ID or the workspace-local latest session",
    )
    session_modes.add_argument(
        "--replay",
        metavar="SESSION_ID",
        help="show a read-only event summary for a session ID or latest",
    )
    session_modes.add_argument(
        "--list-sessions",
        action="store_true",
        help="list durable sessions in the selected workspace",
    )
    session_modes.add_argument(
        "--approvals",
        nargs="?",
        const="all",
        metavar="SESSION_ID",
        help="query approvals for all sessions, a session ID, or latest",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit stable JSON for replay, list-sessions, or approvals",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="include replay event payloads and referenced artifact content",
    )
    parser.add_argument(
        "--approval-action",
        metavar="ACTION",
        help="filter approval query results by action",
    )
    parser.add_argument(
        "--approval-outcome",
        choices=("approved", "denied"),
        help="filter approval query results by outcome",
    )

    parser.add_argument("-m", "--model", help="OpenAI model")
    parser.add_argument("--reasoning-effort", help="none, low, medium, high, xhigh")
    parser.add_argument("--max-turns", help="maximum tool-call turns")
    parser.add_argument(
        "--write",
        action="store_true",
        help="allow the agent to write files inside the workspace",
    )
    parser.add_argument(
        "--auto-approve-edits",
        action="store_true",
        help="apply file patches without interactive approval",
    )
    parser.add_argument(
        "--auto-approve-commands",
        action="store_true",
        help="approve commands automatically only with a pinned Docker sandbox",
    )
    parser.add_argument(
        "--sandbox",
        choices=("none", "auto", "docker"),
        help="sandbox mode (default: auto)",
    )
    parser.add_argument(
        "--sandbox-image",
        metavar="IMAGE",
        help="local Docker image used for sandbox execution",
    )
    parser.add_argument(
        "--full-auto",
        action="store_true",
        help="enable writes and automatic approvals using pinned Docker only",
    )
    parser.add_argument(
        "--max-fix-attempts",
        help=(
            "maximum repair patches allowed after a failed verification "
            "(default: 3, maximum: 10)"
        ),
    )
    parser.add_argument(
        "--context-max-files",
        help="maximum number of file contents sampled into initial context",
    )
    parser.add_argument(
        "--context-max-bytes-per-file",
        help="maximum bytes sampled from each selected file",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        mode = _select_mode(args)
        workspace = Path(args.workspace or os.getcwd()).resolve()

        if mode == "list":
            store = SessionStore(workspace, read_only=True)
            payload = build_session_list_payload(store)
            _print_session_list(payload, as_json=args.json)
            return 0

        if mode == "replay":
            store = SessionStore(workspace, read_only=True)
            session_id = resolve_session_selector(store, args.replay)
            payload = build_session_replay_payload(
                store,
                session_id,
                verbose=args.verbose,
            )
            _print_session_replay(payload, as_json=args.json)
            return 0

        if mode == "approvals":
            store = SessionStore(workspace, read_only=True)
            selector = args.approvals
            if not isinstance(selector, str):
                raise RuntimeError("Approval selector is unavailable.")
            session_ids = _resolve_approval_session_ids(store, selector)
            payload = build_approval_query_payload(
                store,
                session_ids=session_ids,
                selector=selector,
                action=args.approval_action,
                outcome=args.approval_outcome,
            )
            _print_approval_query(payload, as_json=args.json)
            return 0

        if mode == "resume":
            if not os.getenv("OPENAI_API_KEY"):
                raise RuntimeError(
                    "OPENAI_API_KEY is not set. Copy .env.example to .env and fill it in."
                )
            store = SessionStore(workspace)
            session_id = resolve_session_selector(store, args.resume)
            print("coding-agent")
            print(f"workspace: {workspace}")
            print(f"session: {session_id}")
            print("mode: resume")
            report = resume_agent_with_report(
                session_id,
                workspace,
                session_store=store,
            )
            _print_agent_report(report)
            return 0

        config = _preflight_sandbox(load_config(args))
        if not os.getenv("OPENAI_API_KEY"):
            raise RuntimeError(
                "OPENAI_API_KEY is not set. Copy .env.example to .env and fill it in."
            )
        task = " ".join(args.task)
        print("coding-agent")
        print(f"workspace: {config.workspace}")
        print(f"model: {config.model}")
        print(f"mode: {config.permission_mode}")
        print(f"sandbox: {config.sandbox_mode}")

        report = run_agent_with_report(task, config)
        _print_agent_report(report)
        return 0
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1


def _preflight_sandbox(config: AgentConfig) -> AgentConfig:
    """Resolve unattended execution to a pinned local Docker capability."""

    unattended_commands = config.full_auto or config.auto_approve_commands
    if config.sandbox_mode == "none":
        if unattended_commands:
            raise CliUsageError(
                "--full-auto and --auto-approve-commands require a Docker sandbox."
            )
        return config

    backend = DockerSandboxBackend(image_reference=config.sandbox_image)
    if config.sandbox_mode == "auto" and not unattended_commands:
        return config

    capability = backend.probe_capability(config.workspace)
    if not capability.available or capability.image_digest is None:
        raise RuntimeError(
            "Docker sandbox preflight failed: "
            + (capability.reason or "the local image digest is unavailable.")
        )
    if capability.image_reference != config.sandbox_image:
        raise RuntimeError(
            "Docker sandbox preflight returned a different image reference."
        )
    return replace(
        config,
        sandbox_mode="docker",
        sandbox_image_digest=capability.image_digest,
    )


def _select_mode(args: argparse.Namespace) -> CliMode:
    if args.resume is not None:
        mode: CliMode = "resume"
    elif args.replay is not None:
        mode = "replay"
    elif args.list_sessions:
        mode = "list"
    elif args.approvals is not None:
        mode = "approvals"
    else:
        mode = "new"

    if args.verbose and mode != "replay":
        raise CliUsageError("--verbose is only supported with --replay.")
    if (
        args.approval_action is not None or args.approval_outcome is not None
    ) and mode != "approvals":
        raise CliUsageError(
            "--approval-action and --approval-outcome are only supported "
            "with --approvals."
        )

    session_flags = "--resume, --replay, --list-sessions, or --approvals"
    if mode == "new":
        if not args.task:
            raise CliUsageError(f"Provide a task or one of {session_flags}.")
        if args.json:
            raise CliUsageError(
                "--json is only supported with replay, list-sessions, or approvals."
            )
        return mode

    if args.task:
        raise CliUsageError(
            f"A task cannot be combined with {session_flags}."
        )
    if args.json and mode not in {"replay", "list", "approvals"}:
        raise CliUsageError(
            "--json is only supported with replay, list-sessions, or approvals."
        )

    incompatible = [
        flag
        for destination, flag in _NEW_TASK_OPTIONS
        if getattr(args, destination) not in {None, False}
    ]
    if incompatible:
        raise CliUsageError(
            f"{', '.join(incompatible)} may only be used when starting a new task."
        )
    return mode


def _resolve_approval_session_ids(
    store: SessionStore,
    selector: str,
) -> tuple[str, ...]:
    if selector == "all":
        return tuple(summary.session_id for summary in store.list_sessions())
    return (resolve_session_selector(store, selector),)


def _print_agent_report(report: AgentRunReport) -> None:
    if report.verifications:
        print("\nverification")
        for result in report.verifications:
            print(
                f"{result.command_id}: {result.status} "
                f"(attempt {result.attempt}, {result.duration_ms}ms)"
            )
        print(f"final verification status: {report.final_status}")

    print("\nfinal")
    print(report.answer)


def _print_session_list(payload: dict[str, object], *, as_json: bool) -> None:
    if as_json:
        _print_json(payload)
        return

    print("coding-agent sessions")
    print(f"workspace: {payload['workspace']}")
    sessions = payload["sessions"]
    if not isinstance(sessions, list) or not sessions:
        print("no sessions")
        return
    for item in sessions:
        if not isinstance(item, dict):
            continue
        task = item.get("task") or "<task unavailable>"
        print(
            f"{item['session_id']}  {item['status']}  "
            f"{item['updated_at']}  {task}"
        )


def _print_session_replay(payload: dict[str, object], *, as_json: bool) -> None:
    if as_json:
        _print_json(payload)
        return

    session = payload["session"]
    timeline = payload["timeline"]
    if not isinstance(session, dict) or not isinstance(timeline, list):
        raise RuntimeError("Invalid session replay payload.")
    print("coding-agent replay")
    print(f"workspace: {payload['workspace']}")
    print(f"session: {session['session_id']}")
    print(f"status: {session['status']}")
    print(f"final status: {session.get('final_status') or '<not available>'}")
    print(f"task: {session.get('task') or '<task unavailable>'}")
    print("timeline:")
    for item in timeline:
        if not isinstance(item, dict):
            continue
        summary = item.get("summary") or item.get("type")
        print(f"  {item['seq']:>4}  {item['recorded_at']}  {summary}")
        if payload.get("verbose") and "payload" in item:
            rendered = json.dumps(
                item["payload"],
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            for line in rendered.splitlines():
                print(f"        {line}")


def _print_approval_query(payload: dict[str, object], *, as_json: bool) -> None:
    if as_json:
        _print_json(payload)
        return

    approvals = payload.get("approvals")
    filters = payload.get("filters")
    if not isinstance(approvals, list) or not isinstance(filters, dict):
        raise RuntimeError("Invalid approval query payload.")
    print("coding-agent approvals")
    print(f"workspace: {payload['workspace']}")
    print(
        "filters: "
        f"session={filters.get('session') or 'all'}, "
        f"action={filters.get('action') or '*'}, "
        f"outcome={filters.get('outcome') or '*'}"
    )
    if not approvals:
        print("no approvals")
        return
    for item in approvals:
        if not isinstance(item, dict):
            continue
        print(
            f"{item['recorded_at']}  {item['session_id']}  "
            f"{item['action']}  {item['outcome']}({item['source']})  "
            f"{item['summary']}"
        )


def _print_json(payload: dict[str, object]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    raise SystemExit(main())
