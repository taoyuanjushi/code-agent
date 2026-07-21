"use strict";

const path = require("path");

const REVIEW_TASK =
  "Review the current workspace changes and submit a structured review.";

function requireNonEmptyString(value, name) {
  if (typeof value !== "string") {
    throw new TypeError(`${name} must be a string.`);
  }
  if (!value.trim()) {
    throw new Error(`${name} must not be empty.`);
  }
  if (value.includes("\u0000")) {
    throw new Error(`${name} must not contain NUL.`);
  }
  return value;
}

function normalizeWorkspaceRelativePath(value) {
  const candidate = requireNonEmptyString(value, "relativePath").replace(
    /\\/g,
    "/",
  );
  if (
    candidate.startsWith("/") ||
    candidate.startsWith("//") ||
    /^[A-Za-z]:\//.test(candidate)
  ) {
    throw new Error("relativePath must be workspace-relative.");
  }
  const segments = candidate.split("/");
  if (segments.some((segment) => !segment || segment === "." || segment === "..")) {
    throw new Error("relativePath must not contain empty or traversal segments.");
  }
  return segments.join("/");
}

function toWorkspaceRelativePath(workspacePath, filePath) {
  const workspace = requireNonEmptyString(workspacePath, "workspacePath");
  const file = requireNonEmptyString(filePath, "filePath");
  const relativePath = path.relative(workspace, file);
  if (
    !relativePath ||
    path.isAbsolute(relativePath) ||
    relativePath === ".." ||
    relativePath.startsWith(`..${path.sep}`)
  ) {
    throw new Error("The active file must be inside the selected workspace folder.");
  }
  return normalizeWorkspaceRelativePath(relativePath);
}

function buildRunTaskArgs(workspacePath, task) {
  return [
    "--workspace",
    requireNonEmptyString(workspacePath, "workspacePath"),
    "--write",
    requireNonEmptyString(task, "task"),
  ];
}

function buildReviewChangesArgs(workspacePath) {
  return [
    "--workspace",
    requireNonEmptyString(workspacePath, "workspacePath"),
    "--mode",
    "review",
    REVIEW_TASK,
  ];
}

function buildExplainCurrentFileArgs(workspacePath, relativePath) {
  const normalizedPath = normalizeWorkspaceRelativePath(relativePath);
  const renderedPath = JSON.stringify(normalizedPath);
  return [
    "--workspace",
    requireNonEmptyString(workspacePath, "workspacePath"),
    "--mode",
    "explain",
    `Explain the current file at workspace-relative path ${renderedPath}. ` +
      "Cite relevant path:line evidence from that file.",
  ];
}

module.exports = {
  buildExplainCurrentFileArgs,
  buildReviewChangesArgs,
  buildRunTaskArgs,
  normalizeWorkspaceRelativePath,
  toWorkspaceRelativePath,
};