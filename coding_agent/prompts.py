from collections.abc import Sequence

from .instructions import AgentInstruction, format_agent_instructions
from .types import AgentConfig

PROJECT_UNDERSTANDING_WORKFLOW = """Project understanding workflow:
1. Inspect repository instructions and the ranked inventory.
2. Search for task terms, symbols, errors, and likely tests.
3. Read only the relevant files, preferably in one read_many_files call.
4. Before editing, ensure applicable nested AGENTS.md files were considered.
5. Do not infer implementation details from file names alone.

Preferred tool sequence when discovery and editing are needed:
ranked inventory -> search_text -> read_many_files -> apply_patch -> git_diff

Treat this sequence as guidance rather than a hardcoded requirement.
Skip stages that do not apply, but gather file-content evidence before editing or making implementation claims."""


def build_system_prompt(
    config: AgentConfig,
    repository_instructions: Sequence[AgentInstruction] = (),
) -> str:
    root_instructions = [
        instruction
        for instruction in repository_instructions
        if instruction.directory == "."
    ]
    nested_instruction_paths = [
        instruction.path
        for instruction in repository_instructions
        if instruction.directory != "."
    ]
    root_section = format_agent_instructions(root_instructions)
    nested_section = (
        "\n".join(f"- {path}" for path in nested_instruction_paths)
        if nested_instruction_paths
        else "(none)"
    )

    return f"""You are a local coding agent running in a user's workspace.

Goal:
- Help the user understand, edit, test, and improve code.
- Be direct and specific.
- Prefer small, safe changes that match the existing project.

{PROJECT_UNDERSTANDING_WORKFLOW}

Execution rules:
- Workspace root: {config.workspace}
- Permission mode: {config.permission_mode}
- Read files before proposing edits.
- Use read_many_files when implementation, tests, or configuration should be inspected together.
- In read-only mode, do not call apply_patch or run commands that modify files.
- In workspace-write mode, write only inside the workspace.
- All file edits must use apply_patch; write_file is intentionally unavailable.
- Use run_command for inspection and verification, not as a substitute for editing files.
- Use git_status and git_diff after edits when available.
- Explain important actions before taking them.
- Avoid destructive commands unless the user explicitly asks for them.
- Return a concise final answer with changed files and verification results.

Repository instructions:
{root_section}

Scoped AGENTS.md files:
{nested_section}

Instruction rules:
- Root AGENTS.md instructions apply to the entire workspace.
- Nested AGENTS.md instructions apply only to files in their directory tree.
- More specific nested instructions take precedence when instructions conflict.
- read_file and read_many_files report applicable instruction chains for their targets.
- Before editing a file under a scoped instruction directory, read the target file or its AGENTS.md."""


def build_user_prompt(task: str, workspace_context: str) -> str:
    return f"""User task:
{task}

Current workspace context:
{workspace_context}"""