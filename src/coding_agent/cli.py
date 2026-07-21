from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv

from .agent import resume_agent_with_report, run_agent_with_report
from .config import load_config
from .plans import plan_state_from_dict
from .reviews import review_result_from_dict, sorted_review_findings
from .security.docker_backend import DockerSandboxBackend
from .sessions.query import (
    build_session_list_payload,
    build_session_replay_payload,
    resolve_session_selector,
)
from .sessions.reducer import rebuild_state
from .sessions.replay import build_approval_query_payload
from .sessions.store import SessionStore
from .types import AgentConfig
from .ui import JsonlRenderer, TerminalRenderer, UiEmitter

CliMode = Literal["new", "resume", "replay", "list", "approvals"]


class CliUsageError(ValueError):
    """Raised when parsed CLI options do not describe one valid mode."""


class CliConfigurationError(ValueError):
    """Raised when startup configuration is invalid or incomplete."""


_NEW_TASK_OPTIONS = (
    ("task_mode", "--mode"),
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
        "--output",
        choices=("human", "jsonl"),
        default=None,
        help="live output format for new tasks and resume (default: human)",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="disable ANSI color in human live output",
    )
    parser.add_argument(
        "--no-stream",
        action="store_true",
        help="wait for complete model responses instead of streaming output",
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

    parser.add_argument(
        "--mode",
        dest="task_mode",
        choices=("run", "review", "explain"),
        default=None,
        help="task mode for a new task (default: run)",
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
    emitter: UiEmitter | None = None

    try:
        mode = _select_mode(args)
        workspace = Path(args.workspace or os.getcwd()).resolve()
        if mode in {"new", "resume"}:
            emitter = _build_live_emitter(args)

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
                raise CliConfigurationError(
                    "OPENAI_API_KEY is not set. Copy .env.example to .env and fill it in."
                )
            store = SessionStore(workspace)
            session_id = resolve_session_selector(store, args.resume)
            emitter.emit(
                "run.started",
                _resume_run_started_payload(store, session_id, workspace),
            )
            live_options: dict[str, object] = {
                "ui_emitter": emitter,
                "stream": not args.no_stream,
            }
            if args.output == "jsonl":
                live_options["approval_input_reader"] = (
                    _read_jsonl_approval_input
                )
            resume_agent_with_report(
                session_id,
                workspace,
                session_store=store,
                **live_options,
            )
            return _successful_live_exit(emitter)

        try:
            config = load_config(args)
        except (TypeError, ValueError) as exc:
            raise CliConfigurationError(str(exc)) from exc
        config = _preflight_sandbox(config)
        if not os.getenv("OPENAI_API_KEY"):
            raise CliConfigurationError(
                "OPENAI_API_KEY is not set. Copy .env.example to .env and fill it in."
            )
        task = " ".join(args.task)
        emitter.emit(
            "run.started",
            {
                "workspace": config.workspace,
                "model": config.model,
                "mode": config.permission_mode,
                "task_mode": config.task_mode,
                "sandbox": config.sandbox_mode,
            },
        )
        live_options = {
            "ui_emitter": emitter,
            "stream": not args.no_stream,
        }
        if args.output == "jsonl":
            live_options["approval_input_reader"] = (
                _read_jsonl_approval_input
            )
        run_agent_with_report(
            task,
            config,
            **live_options,
        )
        return _successful_live_exit(emitter)
    except (CliUsageError, CliConfigurationError) as exc:
        _print_cli_diagnostic(str(exc), emitter=emitter)
        return 2
    except BrokenPipeError:
        _silence_broken_stdout()
        return 0
    except KeyboardInterrupt:
        if emitter is None:
            print("Interrupted", file=sys.stderr)
        elif not emitter.terminal_event_emitted:
            emitter.emit("run.interrupted", {"reason": "keyboard_interrupt"})
        if emitter is not None and emitter.output_closed:
            _silence_broken_stdout()
        return 130
    except Exception as exc:
        if emitter is not None and emitter.output_closed:
            _silence_broken_stdout()
            return 0
        if emitter is None:
            print(str(exc), file=sys.stderr)
        elif not emitter.terminal_event_emitted:
            emitter.emit("run.failed", {"message": str(exc)})
        if emitter is not None and emitter.output_closed:
            _silence_broken_stdout()
            return 0
        return 1


def _resume_run_started_payload(
    store: SessionStore,
    session_id: str,
    workspace: Path,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "workspace": str(workspace),
        "session_id": session_id,
        "mode": "resume",
    }
    try:
        events = store.load(session_id)
        state = rebuild_state(events)
        started = events[0].payload.get("session", events[0].payload)
        if not isinstance(started, Mapping):
            return payload
        config = started.get("config")
        if not isinstance(config, Mapping):
            return payload
    except Exception:
        # The agent remains authoritative and will report corrupt or legacy state.
        return payload

    payload.update(
        {
            "task_mode": _resume_banner_string(
                config.get("task_mode"),
                "run",
            ),
            "permission": _resume_banner_string(
                config.get("permission_mode"),
                "read-only",
            ),
            "sandbox": _resume_banner_string(
                config.get("sandbox_mode"),
                "none",
            ),
            "previous_phase": state.phase,
            "previous_status": state.status,
            "plan_progress": _plan_progress_payload(state.plan.items),
        }
    )
    model = config.get("model")
    if isinstance(model, str) and model:
        payload["model"] = model
    return payload


def _resume_banner_string(value: object, fallback: str) -> str:
    return value if isinstance(value, str) and value else fallback


def _plan_progress_payload(items: object) -> dict[str, int]:
    values = tuple(items) if isinstance(items, tuple) else ()
    return {
        "completed": sum(getattr(item, "status", None) == "completed" for item in values),
        "in_progress": sum(
            getattr(item, "status", None) == "in_progress" for item in values
        ),
        "pending": sum(getattr(item, "status", None) == "pending" for item in values),
        "total": len(values),
    }


def _successful_live_exit(emitter: UiEmitter) -> int:
    if emitter.output_closed:
        _silence_broken_stdout()
    return 0


def _print_cli_diagnostic(
    message: str,
    *,
    emitter: UiEmitter | None,
) -> None:
    handler = emitter.handler if emitter is not None else None
    diagnostic = getattr(handler, "diagnostic", None)
    if callable(diagnostic):
        diagnostic(message)
        return
    try:
        print(message, file=sys.stderr)
    except BrokenPipeError:
        return


def _silence_broken_stdout() -> None:
    """Prevent interpreter shutdown from re-flushing a known broken pipe."""

    try:
        stdout_fd = sys.stdout.fileno()
        devnull_fd = os.open(os.devnull, os.O_WRONLY)
    except (AttributeError, OSError, ValueError):
        return
    try:
        os.dup2(devnull_fd, stdout_fd)
    except OSError:
        pass
    finally:
        os.close(devnull_fd)


def _build_live_emitter(args: argparse.Namespace) -> UiEmitter:
    if args.output == "jsonl":
        return UiEmitter(JsonlRenderer())
    return UiEmitter(
        TerminalRenderer(color_enabled=not args.no_color)
    )


def _read_jsonl_approval_input(prompt: str) -> str:
    if not isinstance(prompt, str):
        raise TypeError("approval prompt must be a string.")
    sys.stderr.write(prompt)
    sys.stderr.flush()
    return sys.stdin.readline()


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
    if args.output is not None and mode not in {"new", "resume"}:
        raise CliUsageError(
            "--output is only supported with a new task or --resume."
        )
    if args.no_color and mode not in {"new", "resume"}:
        raise CliUsageError(
            "--no-color is only supported with a new task or --resume."
        )
    if args.no_stream and mode not in {"new", "resume"}:
        raise CliUsageError(
            "--no-stream is only supported with a new task or --resume."
        )
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
        task_mode = args.task_mode or "run"
        if task_mode in {"review", "explain"}:
            conflicts = [
                flag
                for destination, flag in (
                    ("write", "--write"),
                    ("auto_approve_edits", "--auto-approve-edits"),
                    ("auto_approve_commands", "--auto-approve-commands"),
                    ("full_auto", "--full-auto"),
                )
                if getattr(args, destination)
            ]
            if conflicts:
                raise CliUsageError(
                    f"--mode {task_mode} cannot be combined with "
                    + ", ".join(conflicts)
                    + "."
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
    terminal = payload.get("terminal")
    if isinstance(terminal, dict):
        print(
            "terminal: "
            f"{terminal.get('status') or session['status']} "
            f"({terminal.get('event_type') or session.get('last_event_type')})"
        )
        reason = terminal.get("reason")
        if isinstance(reason, str) and reason:
            print(f"terminal reason: {reason}")
    else:
        print("terminal: <session is resumable>")
    print(
        "verification status: "
        f"{session.get('final_status') or '<not available>'}"
    )
    print(f"task: {session.get('task') or '<task unavailable>'}")
    _print_replay_plan_updates(payload.get("plan_updates"))
    _print_replay_review(payload.get("review"))
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


def _print_replay_plan_updates(value: object) -> None:
    if not isinstance(value, list):
        return
    print("plan updates:")
    if not value:
        print("  none")
        return
    markers = {
        "pending": "[ ]",
        "in_progress": "[>]",
        "completed": "[x]",
    }
    for update in value:
        if not isinstance(update, dict):
            continue
        plan_data = update.get("plan")
        if not isinstance(plan_data, Mapping):
            continue
        plan = plan_state_from_dict(plan_data, allow_empty=False)
        print(
            f"  {update.get('seq', '?'):>4}  "
            f"{update.get('recorded_at', '<time unavailable>')}"
        )
        if plan.explanation:
            print(f"        {plan.explanation}")
        for item in plan.items:
            print(f"        {markers[item.status]} {item.step}")


def _print_replay_review(value: object) -> None:
    if value is None:
        return
    if not isinstance(value, Mapping):
        raise RuntimeError("Invalid replay review payload.")
    review = review_result_from_dict(value)
    print("review:")
    print(f"  {review.summary}")
    if not review.findings:
        print("  no findings")
        return
    for finding in sorted_review_findings(review):
        print(
            f"  [{finding.severity}] {finding.path}:{finding.line} "
            f"{finding.title}"
        )
        print(f"    {finding.detail}")


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
