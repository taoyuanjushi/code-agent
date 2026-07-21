"use strict";

const assert = require("node:assert/strict");
const path = require("path");
const {
  buildExplainCurrentFileArgs,
  buildReviewChangesArgs,
  buildRunTaskArgs,
  normalizeWorkspaceRelativePath,
  toWorkspaceRelativePath,
} = require("../argv");

const workspace = path.resolve("workspace with spaces");
const file = path.join(workspace, "src", "example.py");

const taskWithShellCharacters = "Fix tests; echo should-not-run";
assert.deepEqual(buildRunTaskArgs(workspace, taskWithShellCharacters), [
  "--workspace",
  workspace,
  "--write",
  taskWithShellCharacters,
]);
assert.deepEqual(buildReviewChangesArgs(workspace), [
  "--workspace",
  workspace,
  "--mode",
  "review",
  "Review the current workspace changes and submit a structured review.",
]);
assert.deepEqual(buildExplainCurrentFileArgs(workspace, "src/example.py"), [
  "--workspace",
  workspace,
  "--mode",
  "explain",
  'Explain the current file at workspace-relative path "src/example.py". ' +
    "Cite relevant path:line evidence from that file.",
]);
assert.deepEqual(buildExplainCurrentFileArgs(workspace, 'src/a"b.py'), [
  "--workspace",
  workspace,
  "--mode",
  "explain",
  'Explain the current file at workspace-relative path "src/a\\"b.py". ' +
    "Cite relevant path:line evidence from that file.",
]);
assert.equal(toWorkspaceRelativePath(workspace, file), "src/example.py");
assert.equal(normalizeWorkspaceRelativePath("src\\example.py"), "src/example.py");
assert.throws(() => normalizeWorkspaceRelativePath("../secret.txt"));
assert.throws(() => normalizeWorkspaceRelativePath("/absolute/file.py"));
assert.throws(() => toWorkspaceRelativePath(workspace, path.dirname(workspace)));
assert.throws(() => buildRunTaskArgs(workspace, "   "));

console.log("argv helper tests passed");