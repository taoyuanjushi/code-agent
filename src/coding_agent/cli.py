import argparse
import os
import sys

from dotenv import load_dotenv

from .agent import run_agent_with_report
from .config import load_config


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="coding-agent",
        description="A local AI coding agent CLI inspired by Codex.",
    )
    parser.add_argument("task", nargs="+", help="coding task to perform")
    parser.add_argument("-w", "--workspace", help="workspace path")
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
        help="run shell commands without interactive approval",
    )
    parser.add_argument(
        "--max-fix-attempts",
        help="maximum repair patches allowed after a failed verification (default: 3, maximum: 10)",
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
        if not os.getenv("OPENAI_API_KEY"):
            raise RuntimeError(
                "OPENAI_API_KEY is not set. Copy .env.example to .env and fill it in."
            )

        config = load_config(args)
        task = " ".join(args.task)

        print("coding-agent")
        print(f"workspace: {config.workspace}")
        print(f"model: {config.model}")
        print(f"mode: {config.permission_mode}")

        report = run_agent_with_report(task, config)
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
        return 0
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
