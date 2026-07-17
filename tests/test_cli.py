from coding_agent.cli import build_parser
from coding_agent.config import load_config


def test_build_parser_accepts_python_main_options() -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "--write",
            "--auto-approve-edits",
            "--auto-approve-commands",
            "--reasoning-effort",
            "medium",
            "--max-fix-attempts",
            "4",
            "fix tests",
        ]
    )

    assert args.write is True
    assert args.auto_approve_edits is True
    assert args.auto_approve_commands is True
    assert args.reasoning_effort == "medium"
    assert args.max_fix_attempts == "4"
    assert args.task == ["fix tests"]


def test_load_config_uses_m2_initial_context_defaults() -> None:
    options = build_parser().parse_args(["inspect"])

    config = load_config(options)

    assert config.context_max_files == 6
    assert config.context_max_bytes_per_file == 8_000
    assert config.max_fix_attempts == 3
    assert config.sandbox_mode == "auto"
    assert config.sandbox_image == "python:3.12-slim"
    assert config.sandbox_image_digest is None
    assert config.full_auto is False
