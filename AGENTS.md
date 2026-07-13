# Repository Guidelines

## Project Structure & Module Organization

The Python package lives in `coding_agent/`. `cli.py` provides the command-line entry point, `agent.py` owns the model/tool loop, and modules such as `patch.py`, `path_safety.py`, `search.py`, and `tools.py` isolate filesystem and command behavior. Tests mirror these modules under `tests/` as `test_<module>.py`. Design notes belong in `docs/`; packaging and test configuration are in `pyproject.toml`. Generated files, virtual environments, logs, and secrets must remain untracked according to `.gitignore`.

## Build, Test, and Development Commands

Use Python 3.12 or newer. On Windows PowerShell:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
```

- `python -m coding_agent "analyze this repository"` runs the CLI in read-only mode.
- `python -m coding_agent --write "task"` permits workspace edits; bypass approvals only in controlled environments.
- `python -m pytest` runs the full test suite.
- `python -m pytest tests/test_patch.py -q` runs one focused test module.
- `python -m pip wheel . -w dist` verifies wheel packaging.

## Coding Style & Naming Conventions

Use four-space indentation, `snake_case` for modules/functions/variables, and `PascalCase` for classes. Add type annotations to public functions and prefer small, single-purpose modules. Group imports as standard library, third-party, then local package imports. No formatter or linter is configured, so match the existing PEP 8-oriented style and avoid unrelated formatting changes.

## Testing Guidelines

Tests use `pytest`, fixtures such as `tmp_path`, and mocked clients to avoid real API calls. Name tests `test_<behavior>` and cover successful operations plus safety failures, especially workspace path validation, patch application, command permissions, and CLI parsing. No coverage threshold is configured; new behavior should include regression tests and should not require `OPENAI_API_KEY` unless testing integration behavior.

## Commit & Pull Request Guidelines

The repository has no commit history, so no convention can be inferred. Use concise, imperative subjects such as `Add patch validation for deleted files`, and keep commits focused. Pull requests should explain the change and risk, list verification commands, link issues, and include sample CLI output for behavior changes. Highlight security-sensitive changes to path handling, shell execution, approvals, or configuration.

## Security & Configuration Tips

Never commit `.env` or API keys. Add settings to `.env.example` with blank or safe placeholders. Preserve the default read-only posture and ensure file operations resolve inside the configured workspace.
