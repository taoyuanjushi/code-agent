# Coding Agent VS Code Prototype

This directory contains the minimal M6 VS Code prototype. It has no runtime npm dependencies and launches the existing `coding-agent` CLI as a VS Code task backed by `ProcessExecution`.

## Development Host

1. Install the Python package and make `coding-agent` available on `PATH`.
2. Open `editors/vscode` as a VS Code workspace.
3. Press `F5` and select `Run Coding Agent Extension`.
4. In the Extension Development Host, open the command palette and run one of:
   - `Coding Agent: Run Task`
   - `Coding Agent: Review Changes`
   - `Coding Agent: Explain Current File`

Set `codingAgent.executable` when the executable is not on `PATH`. The value is passed directly to `ProcessExecution`; it is an executable path, not a shell command line.

## Behavior

- Run Task starts the CLI with `--write`, so normal edit and command approvals remain active.
- Review Changes uses `--mode review`.
- Explain Current File uses `--mode explain` and passes only the selected workspace folder plus the active file's workspace-relative path. Editor selection text is never placed in process arguments.
- A multi-root workspace always shows the workspace-folder picker.
- Each command runs in a dedicated VS Code task terminal, preserving CLI output, approval input, Ctrl+C, and exit codes.

## Test

The argv helper uses only Node built-ins:

```powershell
node .\test\argv.test.js
```

Repository-level static coverage is in `tests/test_m6_step13.py` and does not require VS Code to be installed.