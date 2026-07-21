from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
EXTENSION_ROOT = REPOSITORY_ROOT / "editors" / "vscode"
PACKAGE_PATH = EXTENSION_ROOT / "package.json"
EXTENSION_PATH = EXTENSION_ROOT / "extension.js"
ARGV_PATH = EXTENSION_ROOT / "argv.js"
COMMANDS = {
    "codingAgent.runTask": "Coding Agent: Run Task",
    "codingAgent.reviewChanges": "Coding Agent: Review Changes",
    "codingAgent.explainCurrentFile": "Coding Agent: Explain Current File",
}


def _package() -> dict[str, object]:
    return json.loads(PACKAGE_PATH.read_text(encoding="utf-8"))


def test_vscode_manifest_declares_only_the_minimal_prototype() -> None:
    package = _package()
    contributes = package["contributes"]
    assert isinstance(contributes, dict)
    commands = contributes["commands"]
    assert isinstance(commands, list)

    assert package["main"] == "./extension.js"
    assert package["private"] is True
    assert package["extensionKind"] == ["workspace"]
    assert package.get("dependencies", {}) == {}
    assert package.get("devDependencies", {}) == {}
    assert {
        item["command"]: item["title"]
        for item in commands
        if isinstance(item, dict)
    } == COMMANDS
    assert set(package["activationEvents"]) == {
        f"onCommand:{command_id}" for command_id in COMMANDS
    }

    configuration = contributes["configuration"]
    assert isinstance(configuration, dict)
    properties = configuration["properties"]
    assert isinstance(properties, dict)
    executable = properties["codingAgent.executable"]
    assert executable["type"] == "string"
    assert executable["default"] == "coding-agent"


def test_vscode_extension_uses_process_execution_and_workspace_selection() -> None:
    source = EXTENSION_PATH.read_text(encoding="utf-8")

    for command_id in COMMANDS:
        assert command_id in source
    assert "new vscode.ProcessExecution(executable, args, {" in source
    assert "cwd: folder.uri.fsPath" in source
    assert "vscode.tasks.executeTask(task)" in source
    assert "vscode.TaskPanelKind.Dedicated" in source
    assert "showWorkspaceFolderPick" in source
    assert "vscode.workspace.isTrusted" in source
    assert "folders.length === 1" in source
    assert "folder.uri.fsPath" in source
    assert ".selection" not in source
    assert ".getText(" not in source

    forbidden = (
        "ShellExecution",
        "createTerminal(",
        ".sendText(",
        "shell: true",
        "child_process",
        "exec(",
        "spawn(",
        'args.join(" ")',
        "args.join(' ')",
    )
    assert all(token not in source for token in forbidden)


def test_vscode_argv_helper_keeps_workspace_and_task_as_separate_argv() -> None:
    source = ARGV_PATH.read_text(encoding="utf-8")

    assert '"--workspace"' in source
    assert '"--write"' in source
    assert '"--mode"' in source
    assert '"review"' in source
    assert '"explain"' in source
    assert "toWorkspaceRelativePath" in source
    assert r"/\\/g" in source
    assert "relativePath.startsWith(`..${path.sep}`)" in source
    assert "selection" not in source.lower()
    assert "getText" not in source


def test_vscode_development_host_configuration_is_self_contained() -> None:
    launch_path = EXTENSION_ROOT / ".vscode" / "launch.json"
    launch = json.loads(launch_path.read_text(encoding="utf-8"))
    configurations = launch["configurations"]

    assert len(configurations) == 1
    assert configurations[0]["type"] == "extensionHost"
    assert configurations[0]["request"] == "launch"
    assert configurations[0]["args"] == [
        "--extensionDevelopmentPath=${workspaceFolder}"
    ]
    assert (EXTENSION_ROOT / "README.md").is_file()
    assert (EXTENSION_ROOT / "test" / "argv.test.js").is_file()


@pytest.mark.local_node
def test_vscode_argv_helper_node_smoke() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is not installed.")

    completed = subprocess.run(
        [node, str(EXTENSION_ROOT / "test" / "argv.test.js")],
        cwd=EXTENSION_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "argv helper tests passed"