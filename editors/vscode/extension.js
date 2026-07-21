"use strict";

const vscode = require("vscode");
const {
  buildExplainCurrentFileArgs,
  buildReviewChangesArgs,
  buildRunTaskArgs,
  toWorkspaceRelativePath,
} = require("./argv");

const COMMANDS = Object.freeze({
  runTask: "codingAgent.runTask",
  reviewChanges: "codingAgent.reviewChanges",
  explainCurrentFile: "codingAgent.explainCurrentFile",
});

function activate(context) {
  context.subscriptions.push(
    registerCommand(COMMANDS.runTask, runTask),
    registerCommand(COMMANDS.reviewChanges, reviewChanges),
    registerCommand(COMMANDS.explainCurrentFile, explainCurrentFile),
  );
}

function deactivate() {}

function registerCommand(commandId, callback) {
  return vscode.commands.registerCommand(commandId, async () => {
    try {
      await callback();
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      await vscode.window.showErrorMessage(`Coding Agent: ${message}`);
    }
  });
}

async function runTask() {
  const folder = await selectWorkspaceFolder();
  if (!folder) {
    return;
  }
  const task = await vscode.window.showInputBox({
    title: "Coding Agent: Run Task",
    prompt: "Describe the coding task to run in the selected workspace.",
    placeHolder: "Fix the failing tests and verify the changes",
    ignoreFocusOut: true,
  });
  if (task === undefined) {
    return;
  }
  if (!task.trim()) {
    await vscode.window.showWarningMessage("Coding Agent: Task must not be empty.");
    return;
  }
  await executeCodingAgentTask(
    folder,
    COMMANDS.runTask,
    "Coding Agent: Run Task",
    buildRunTaskArgs(folder.uri.fsPath, task),
  );
}

async function reviewChanges() {
  const folder = await selectWorkspaceFolder();
  if (!folder) {
    return;
  }
  await executeCodingAgentTask(
    folder,
    COMMANDS.reviewChanges,
    "Coding Agent: Review Changes",
    buildReviewChangesArgs(folder.uri.fsPath),
  );
}

async function explainCurrentFile() {
  const folder = await selectWorkspaceFolder();
  if (!folder) {
    return;
  }
  const editor = vscode.window.activeTextEditor;
  if (!editor || editor.document.uri.scheme !== "file") {
    await vscode.window.showErrorMessage(
      "Coding Agent: Open a local workspace file before explaining it.",
    );
    return;
  }
  const relativePath = toWorkspaceRelativePath(
    folder.uri.fsPath,
    editor.document.uri.fsPath,
  );
  await executeCodingAgentTask(
    folder,
    COMMANDS.explainCurrentFile,
    "Coding Agent: Explain Current File",
    buildExplainCurrentFileArgs(folder.uri.fsPath, relativePath),
  );
}

async function selectWorkspaceFolder() {
  if (!vscode.workspace.isTrusted) {
    await vscode.window.showErrorMessage(
      "Coding Agent: Trust this workspace before starting the CLI.",
    );
    return undefined;
  }
  const folders = vscode.workspace.workspaceFolders;
  if (!folders || folders.length === 0) {
    await vscode.window.showErrorMessage(
      "Coding Agent: Open a workspace folder before running a command.",
    );
    return undefined;
  }
  if (folders.length === 1) {
    return folders[0];
  }
  return vscode.window.showWorkspaceFolderPick({
    placeHolder: "Select the workspace folder for Coding Agent",
  });
}

async function executeCodingAgentTask(folder, commandId, title, args) {
  const executable = vscode.workspace
    .getConfiguration("codingAgent", folder.uri)
    .get("executable", "coding-agent");
  if (typeof executable !== "string" || !executable.trim()) {
    throw new Error("codingAgent.executable must be a non-empty string.");
  }
  if (executable.includes("\u0000")) {
    throw new Error("codingAgent.executable must not contain NUL.");
  }

  const execution = new vscode.ProcessExecution(executable, args, {
    cwd: folder.uri.fsPath,
  });
  const task = new vscode.Task(
    { type: "coding-agent", command: commandId },
    folder,
    title,
    "coding-agent",
    execution,
    [],
  );
  task.presentationOptions = {
    reveal: vscode.TaskRevealKind.Always,
    panel: vscode.TaskPanelKind.Dedicated,
    focus: true,
    echo: true,
    clear: false,
    showReuseMessage: true,
  };
  await vscode.tasks.executeTask(task);
}

module.exports = {
  activate,
  deactivate,
};