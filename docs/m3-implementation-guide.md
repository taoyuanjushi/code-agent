# M3 阶段详细实现指南：验证闭环

## 1. 阶段目标

M2 已经解决“如何理解仓库、如何找到相关文件”。M3 继续解决“如何证明修改有效、失败后如何继续修复”。完成本阶段后，代理应能在小型 Python 或 TypeScript 项目中完成：

```text
发现项目验证命令
    ↓
选择最相关的 test / lint / typecheck / build
    ↓
执行并返回结构化结果
    ↓
压缩失败输出，保留可定位的错误证据
    ↓
搜索 → 读取 → apply_patch
    ↓
重新执行失败检查，再运行更宽的验证
    ↓
输出最终验证摘要
```

M3 不负责完整沙箱、会话持久化或流式 UI；这些分别属于 M5、M4 和 M6。

## 2. 学习目标与先修知识

建议先掌握：

- `subprocess`、`shell=False`、参数数组、返回码和超时。
- `pathlib`、JSON 配置读取和跨平台命令构造。
- `dataclass`、`Literal`、枚举状态和不可变结果对象。
- `stdout`、`stderr`、ANSI 控制符、按行截断和错误上下文提取。
- pytest fixture、`monkeypatch`、fake client 和临时仓库。
- 当前代理循环：`agent.py → tools.py → subprocess → ToolResult`。
- M2 的工作流：文件清单 → `search_text` → `read_many_files` → `apply_patch`。

先运行基线测试，确认 M2 没有回归：

```powershell
.\.venv\Scripts\python.exe -m pytest -q --basetemp=.coding-agent\pytest-m3-baseline
```

以后每完成一个步骤，都先运行新增的聚焦测试，再运行完整测试。

## 3. 设计原则

### 3.1 发现和执行分离

不要让模型直接拼接“猜出来的” shell 字符串。发现阶段只返回项目文件中明确存在的命令，执行阶段只接受发现结果中的稳定 ID：

```text
discover_verification_commands() -> [VerificationCommand]
run_verification(command_id="python:pytest")
```

现有 `run_command` 继续保留，用于用户明确要求的通用命令；M3 的验证工具不应通过 `run_command` 绕过命令发现。

### 3.2 命令参数使用数组

内部统一使用 `tuple[str, ...]`，例如：

```python
(sys.executable, "-m", "pytest", "-q")
("npm", "run", "test")
```

执行时使用 `subprocess.run(argv, shell=False, ...)`。不要把用户任务、脚本名或文件路径拼成一个 shell 字符串。M5 再增加更严格的 allowlist 和沙箱。

### 3.3 输出限制必须是硬限制

测试失败可能打印大量日志。压缩器应同时限制：

- 最大输出字节数，例如 `32 KiB`；
- 最大输出行数，例如 `200` 行；
- 每个错误上下文窗口的行数；
- stdout 和 stderr 的总预算。

结果中要注明是否截断、丢弃了多少行和字节，不能只返回一个没有解释的片段。

### 3.4 失败结果是上下文，不是最终答案

验证失败后，模型必须把错误摘要当作新的检索线索：先搜索错误符号或文件路径，再读取相关源码和测试，然后修改并重新验证。不要把整段原始日志反复放进后续请求。

## 4. M3 的建议接口

新增 `src/coding_agent/verification.py`，集中放置发现、选择和结果模型；新增 `src/coding_agent/output.py` 或在该模块内放置输出压缩逻辑。建议类型如下：

```python
from dataclasses import dataclass
from typing import Literal

VerificationKind = Literal["test", "lint", "typecheck", "build"]
VerificationStatus = Literal[
    "passed", "failed", "timed_out", "not_found", "error"
]

@dataclass(frozen=True)
class VerificationCommand:
    id: str                         # 例如 python:pytest 或 node:test
    kind: VerificationKind
    argv: tuple[str, ...]
    cwd: str
    source: str                     # 例如 pyproject.toml 或 package.json#scripts.test
    available: bool

@dataclass(frozen=True)
class VerificationResult:
    command_id: str
    kind: VerificationKind
    status: VerificationStatus
    argv: tuple[str, ...]
    cwd: str
    exit_code: int | None
    duration_ms: int
    output: str
    truncated: bool
    omitted_lines: int
    omitted_bytes: int
    attempt: int
```

命令对象描述“可以执行什么”，结果对象描述“这次执行发生了什么”。两者不要混为一个字符串。

## 5. 详细实现步骤

### 5.1 第一步：先固定 M3 验收指标

不要先修改代理循环。先增加测试文件和验收常量，固定以下结果：

- Python fixture 能发现 `pytest`，并识别为 `test` 命令。
- TypeScript fixture 能从 `package.json` 发现 `test`、`lint` 和 `build` scripts。
- 发现结果包含稳定 ID、命令来源、工作目录和可用性。
- 发现结果不包含 `install`、`publish`、`deploy` 等依赖安装或发布命令。
- 命令执行使用参数数组，测试中断言不会经过 shell 字符串拼接。
- 通过、失败、超时和命令不存在都有不同的结构化状态。
- 失败输出不超过 `32 * 1024` 字节和 `200` 行，并保留错误行及其上下文。
- 失败摘要中包含退出码、命令 ID、截断信息和可行动的错误片段。
- fake client 能走完“发现 → 读取 → 修改 → 验证失败 → 再读取 → 再修改 → 验证通过”。
- 最终报告至少包含每次验证的状态、命令、耗时和最终结论。

建议把数值写成测试常量，例如：

```python
M3_MAX_OUTPUT_BYTES = 32 * 1024
M3_MAX_OUTPUT_LINES = 200
M3_MAX_REPAIR_ATTEMPTS = 3
```

测试必须固定这些值，不要依赖默认值的偶然变化。

### 5.2 第二步：定义验证领域模型

在 `src/coding_agent/verification.py` 中实现 `VerificationCommand`、`VerificationResult` 和发现结果容器。

实现要求：

1. `argv` 使用 tuple，防止调用方在执行前悄悄修改参数。
2. `cwd` 必须是 workspace 内的绝对路径，并复用 `path_safety.py` 的校验。
3. `id` 由命令类型、项目文件和脚本名组成，不能使用随机 UUID。
4. `source` 保留发现依据，便于模型解释“为什么运行这个命令”。
5. `available=False` 时仍可返回命令，但执行工具应给出明确的缺少运行时或依赖提示。
6. `VerificationResult` 不保存完整原始日志，只保存压缩后的输出和统计字段。

为状态转换写测试，至少覆盖：`passed`、`failed`、`timed_out`、`not_found` 和 `error`。

### 5.3 第三步：实现 Python 项目命令发现

优先读取 workspace 根目录的配置，不要递归扫描整个仓库。第一版支持：

| 依据 | 可发现的命令 | 规则 |
| --- | --- | --- |
| `pyproject.toml` 的 pytest 配置或 `tests/` 目录 | test | 优先 `sys.executable -m pytest -q` |
| `ruff` 配置或开发依赖 | lint | `sys.executable -m ruff check .` |
| `mypy` 配置或开发依赖 | typecheck | `sys.executable -m mypy .` |
| 可构建的 `pyproject.toml` | build | `sys.executable -m build` |
| `pytest.ini`、`tox.ini`、`setup.cfg` | test | 只作为 pytest 存在的配置证据 |

建议顺序：

1. 用 `tomllib` 读取 `pyproject.toml`；解析失败时返回发现错误，不要让整个代理崩溃。
2. 检查 `project.optional-dependencies.dev`、相关 tool 配置和 `tests/` 目录。
3. 用 `importlib.util.find_spec` 或 `shutil.which` 判断运行时是否可用。
4. 使用当前 Python 解释器 `sys.executable`，不要硬编码 `python` 或 `py`。
5. 对同一 `kind` 去重，保留证据最强、命令最具体的条目。
6. 结果按 `test → lint → typecheck → build` 稳定排序。

第一版不要猜测 `make test`、`poetry run` 或任意 README 代码块中的命令。可以在 `source` 中标记“由 tests 目录推断”，但不能伪造配置来源。

### 5.4 第四步：实现 TypeScript 项目命令发现

读取根目录 `package.json`，只解析 `scripts` 对象。第一版支持 npm、pnpm 和 yarn：

1. 先根据 `packageManager` 字段判断包管理器。
2. 没有该字段时，按 `pnpm-lock.yaml`、`yarn.lock`、`package-lock.json` 的顺序判断。
3. 都不存在时回退为 `npm`，并把回退原因写入发现结果。
4. `scripts.test` 映射到 `test`，`scripts.lint` 映射到 `lint`，`scripts.build` 映射到 `build`。
5. `scripts.typecheck` 或 `scripts["type-check"]` 映射到 `typecheck`。
6. 命令使用数组，例如 `("npm", "run", "test")`，不把脚本内容展开到 shell。
7. 每条命令的 `source` 使用 `package.json#scripts.test` 这样的路径。

只支持根 package manifest，暂不处理 monorepo 的递归 workspace；如果检测到多个 workspace，报告提示并保留根脚本。不要自动执行 `npm install` 或修改 lockfile。

### 5.5 第五步：实现稳定选择和相关性排序

发现多个检查后，工具应提供稳定、可解释的排序：

- 用户任务明确提到“测试/失败测试”时，`test` 排在最前。
- 提到 lint、格式化、类型错误时，相关 kind 优先。
- 最近一次失败的 command ID 优先重跑。
- 修复代码后先跑快速 test/lint，再跑 typecheck/build。
- 同一 kind 的命令按 `id` 字典序排序。

不要在 M3 引入 embedding。将选择原因作为字段返回，例如 `reason: "task mentions test"` 或 `reason: "previous attempt failed"`，这样测试可以验证排序不是偶然的。

> 实现状态：已完成。`rank_verification_commands` 会按最近失败命令、修改后快速检查、任务关键词、kind 默认顺序和 command ID 依次排序，并为每条命令返回 `reason`。相关单元测试覆盖中英文关键词、稳定次序和优先级覆盖。

### 5.6 第六步：实现受控验证执行器

在 `verification.py` 中实现 `run_verification_command`，或拆出 `command_runner.py`。不要直接复用当前 `_run_shell_command`，因为它接收 shell 字符串；可以复用超时、批准和 workspace 检查的策略，但底层执行必须改为参数数组。

执行器需要：

1. 根据 `command_id` 查找本次发现结果，不接受任意 argv。
2. 检查 command 的 `cwd` 是否仍在 workspace 内。
3. 执行前复用现有命令批准流程；`--auto-approve-commands` 只跳过交互确认，不改变命令来源校验。
4. 设置 `text=True`、`encoding="utf-8"`、`errors="replace"`，避免单个坏字节导致报告失败。
5. 使用 `time.monotonic()` 统计耗时。
6. 超时后返回 `timed_out`，包含已收集的有限输出，不抛出未处理的 `TimeoutExpired`。
7. 运行时缺少时返回 `not_found`；其他启动异常返回 `error`。
8. 保留 stdout 和 stderr 的来源标记，再交给压缩器合并。

此步骤只运行已有依赖，不安装依赖、不修改 package manifest、不执行发布命令。

### 5.7 第七步：实现失败输出压缩

新增纯函数，例如：

```python
def summarize_command_output(
    stdout: str,
    stderr: str,
    *,
    max_bytes: int,
    max_lines: int,
) -> OutputSummary: ...
```

建议算法：

1. 去除 ANSI 颜色和光标控制符，统一换行符为 `\n`。
2. 为每行加上 `stdout:` 或 `stderr:` 来源，但统计时按最终 UTF-8 字节计算。
3. 优先保留包含 `error`、`failed`、`failure`、`traceback`、`assert`、`exception`、文件路径和行号的行。
4. 对每个命中行保留前后少量上下文。
5. 再补充输出开头和结尾，保证没有错误关键词时也有可读信息。
6. 去重重叠窗口，按原始出现顺序输出。
7. 超过行数或字节预算时截断，并计算 `omitted_lines`、`omitted_bytes`。
8. 失败结果至少保留退出码和 stderr 末尾；通过结果只保留简短摘要。

不要用简单的 `output[-32768:]` 代替错误提取，因为真正的错误通常位于输出中间。

测试至少覆盖：空输出、短输出、彩色输出、超长 traceback、错误在中间、stdout/stderr 混合、Unicode 和 CRLF。

> 实现状态：已完成。`OutputSummary` 和 `summarize_command_output` 会清理 ANSI 控制序列、标记输出来源、优先保留错误命中行及上下文、stderr 尾部和全局首尾，并精确记录 `omitted_lines` 与 `omitted_bytes`。执行器已接入该压缩器，成功结果另外限制为 20 行和 4 KiB。

### 5.8 第八步：增加工具 schema 和结构化返回

在 `src/coding_agent/tools.py` 增加两个工具：

- `discover_verification_commands`：返回所有发现的命令、来源、可用性和排序原因。
- `run_verification`：参数只包含 `command_id`、可选 `timeout_ms` 和可选输出预算。

工具层要求：

- `command_id` 必须是非空字符串，并且必须来自当前 workspace 的发现结果。
- 超时和输出预算复用统一的硬上限校验。
- 未找到命令、命令不可用和执行失败都返回 `ok=False` 或明确的状态数据；不要把失败伪装成工具调用异常。
- 保持现有 `ToolResult` 的兼容性。推荐增加可选的 `data: dict[str, object] | None` 字段，并继续提供面向模型的 `output` 文本。
- agent 发送工具结果时，同时序列化 `ok`、`output` 和结构化 `data`。

一个验证结果的模型输入可以是：

```json
{
  "ok": false,
  "output": "pytest failed: 1 failed, 2 passed",
  "data": {
    "type": "verification_result",
    "command_id": "python:pytest",
    "status": "failed",
    "exit_code": 1,
    "duration_ms": 842,
    "truncated": false,
    "attempt": 1
  }
}
```

> 实现状态：已完成。工具 schema 禁止传入任意 `argv`，仅允许通过发现结果中的 `command_id` 执行；`ToolResult.data` 会返回发现列表、执行状态、attempt、截断统计和修复上限状态。

### 5.9 第九步：更新提示词和代理状态

更新 `prompts.py` 的工作流，明确以下顺序：

1. 先调用 `discover_verification_commands`，不要猜命令。
2. 选择与任务最相关且可用的检查。
3. 修改前先搜索并读取证据。
4. `apply_patch` 成功后，至少运行一次相关验证。
5. 验证失败时，提取错误路径、符号和行号，重新搜索并读取。
6. 修复后重跑失败命令；通过后再运行更宽的检查。
7. 最终回答必须说明运行了哪些命令、哪些通过、哪些跳过及原因。

在 `agent.py` 中增加内存中的验证历史。推荐保留现有 `run_agent(...) -> str` 兼容接口，再增加：

```python
def run_agent_with_report(...) -> AgentRunReport: ...
```

其中 `AgentRunReport` 至少包含最终答案和 `tuple[VerificationResult, ...]`。CLI 可以在最终回答前打印验证摘要；M4 再将相同数据写入 JSONL。

为避免无限循环，增加独立于 `max_turns` 的 `max_fix_attempts`，默认值建议为 `3`，并设置不可被 CLI 无限放大的硬上限。达到上限后要明确告诉模型和用户“仍有验证失败”，不能继续盲目修改。

> 实现状态：已完成。系统提示明确要求先发现命令、修改后验证、失败后按错误证据重新搜索；`run_agent_with_report` 保留顺序化验证历史并计算 `passed`、`failed` 或 `not_run` 最终状态。CLI 支持 `--max-fix-attempts`，默认 3、硬上限 10。

### 5.10 第十步：实现迭代修复循环

先用 fake client 编排一个确定的流程，而不是直接依赖真实模型验证：

```text
初始响应
  → discover_verification_commands
  → search_text
  → read_many_files
  → apply_patch
  → run_verification (failed)
  → search_text(error symbol)
  → read_many_files
  → apply_patch
  → run_verification (passed)
  → final response
```

循环规则：

- 只有 `apply_patch` 成功后才增加 repair attempt。
- 同一失败 command ID 优先重跑，避免每次从头执行所有检查。
- 连续失败达到 `max_fix_attempts` 时停止自动修复。
- 工具失败、命令不可用和测试失败必须区分；只有测试失败才进入“读取错误并修复”路径。
- 验证通过后不要为了“看起来完整”无限运行重复命令。
- 最终报告按执行顺序记录每次验证，而不是只保留最后一次。

不要把“模型最终说修好了”当作通过标准；只有结构化验证状态为 `passed` 才能在报告中写“已验证”。

> 实现状态：已完成。`VerificationToolState` 只在失败后成功应用补丁时增加修复次数；同一命令通过后清除失败状态，无新编辑时不会重复执行已通过命令，达到上限后拒绝继续打补丁。fake client 集成测试覆盖“失败 → 重新搜索/读取 → 再次补丁 → 通过”。

### 5.11 第十一步：补齐测试矩阵

建议新增以下测试模块：

```text
tests/test_verification_discovery.py
 tests/test_verification_runner.py
 tests/test_output_summary.py
 tests/test_verification_tools.py
 tests/test_agent_verification.py
 tests/test_m3_integration.py
 tests/fixtures/m3_python_project/
 tests/fixtures/m3_typescript_project/
```

测试重点：

1. Python：只存在 `tests/`、存在 pytest 配置、存在 ruff/mypy 配置、无可用依赖。
2. TypeScript：npm/pnpm/yarn lockfile、缺失脚本、非字符串脚本、无 package.json。
3. 发现结果排序稳定，重复调用输出完全一致。
4. `shell=False` 和参数数组：通过 monkeypatch `subprocess.run` 断言参数形状。
5. 超时、启动失败、非零退出码和 Unicode 输出。
6. 输出压缩的行数、字节数、错误上下文和 omitted 统计。
7. command ID 篡改、workspace 逃逸和任意命令注入被拒绝。
8. fake client 能完成一次失败后修复并通过的循环。
9. 没有 Node.js 的环境也能运行 TypeScript 命令发现单测；真实 Node smoke test 使用可选 marker，不得成为默认测试依赖。

中型集成测试应动态创建一个小仓库，包含：

- Python 或 TypeScript 的失败测试；
- 一个相似但无关的源码文件；
- 根目录 `AGENTS.md`；
- 生成目录和日志文件；
- 第一次补丁故意不完整，第二次补丁才能通过。

集成测试必须断言代理读取的是失败输出指向的目标文件，而不是依赖固定文件名猜测。

> 实现状态：已完成。验证发现、执行、压缩、工具接入和代理修复循环均有独立回归测试；README、总实施计划和本指南同步为 M3 已完成。

### 5.12 第十二步：更新文档、验收和小提交

每个步骤完成后保持测试通过，推荐提交顺序：

1. `test: define M3 verification acceptance metrics`
2. `feat: add verification command models`
3. `feat: discover Python verification commands`
4. `feat: discover TypeScript package scripts`
5. `feat: run discovered commands safely`
6. `feat: summarize verification output`
7. `feat: expose structured verification tools`
8. `feat: add iterative repair report`
9. `test: add M3 integration matrix`
10. `docs: document M3 verification loop`

本仓库目前尚无 Git 提交，因此这些是建议的主题格式，不是已有历史约定。每次提交前运行：

```powershell
.\.venv\Scripts\python.exe -m pytest -q --basetemp=.coding-agent\pytest-m3
python -m pip wheel . -w dist
```

## 6. 最小可运行示例

假设项目包含 `tests/test_refund.py`，并且 `pyproject.toml` 声明 pytest：

```python
commands = discover_verification_commands(workspace)
# [VerificationCommand(id="python:pytest", kind="test", ...)]

result = run_verification_command(
    workspace,
    command_id="python:pytest",
    max_output_bytes=32 * 1024,
    max_output_lines=200,
)

assert result.status in {"passed", "failed", "timed_out"}
```

模型看到失败摘要后，应搜索失败测试名称或 traceback 中的符号，再通过 `read_many_files` 读取实现和测试，最后调用 `apply_patch`。模型只有在下一次 `run_verification` 返回 `passed` 时，才能报告“修复并通过测试”。

## 7. 常见误区

### 7.1 把 `package.json` 的 script 内容直接交给 shell

script 内容可能包含任意 shell 语法。发现器只负责识别脚本名，执行器应调用包管理器的固定 argv；脚本内部的 shell 行为属于后续安全策略问题。

### 7.2 没有发现结果也允许执行任意 command ID

这会让 `run_verification` 退化为另一个 `run_command`。必须重新发现并匹配 ID。

### 7.3 只截取日志尾部

错误可能出现在中间，尾部往往只有测试汇总。应保留错误关键词及上下文，并返回截断统计。

### 7.4 把所有输出放进下一次模型请求

输出压缩的目的就是控制上下文。只发送摘要、命令元数据和可定位的错误线索；必要时让模型重新搜索完整日志中的文件名和符号。

### 7.5 用一个布尔值表示验证结果

`False` 不能区分测试失败、超时、依赖缺失、用户拒绝和工具异常。必须使用结构化状态。

### 7.6 把“执行过命令”当成“代码已验证”

命令返回码、测试框架结果和是否运行了修改后的相关检查都要记录。只运行了 `git diff` 不能算验证通过。

## 8. 调试与排错

### 发现不到 Python 测试

检查 workspace 根目录是否有 `tests/` 或 pytest 配置，确认使用的是当前 `sys.executable`，并在结果中显示 `source` 和 `available`。

### npm 命令在 Windows 上行为不一致

不要使用 `shell=True`。调用 `npm.cmd` 的平台适配应集中在命令构造函数，并用 monkeypatch 测试 Windows 分支；命令 ID 和脚本来源保持不变。

### 输出压缩后没有错误行

先单独测试 ANSI 清理、错误关键词匹配和窗口去重，再检查最终 UTF-8 字节预算；不要在最后一步无条件截断掉已选中的错误行。

### 代理在失败后重复同一命令

检查 `VerificationResult` 是否带有 `attempt` 和 `command_id`，并让提示词明确“先读取错误证据再修改”；达到修复次数上限后应停止。

### 完整测试偶尔超过超时

不要随意增大默认 timeout。先区分测试命令本身慢、子进程未退出和输出压缩阻塞，再用专门的超时测试复现。

## 9. 面试会怎么问

### 问：为什么自动发现命令不能直接读取 README 中的代码块？

答：README 中的命令可能过时、面向不同平台或包含破坏性操作。M3 应优先使用 `pyproject.toml`、`package.json` 等机器可读配置；README 只能作为未来可选提示源，不能成为默认执行依据。

### 问：为什么发现和执行要拆开？

答：发现阶段产生可审计的候选命令，执行阶段只接受候选命令 ID。这样可以阻止模型通过“验证工具”偷偷传入任意 shell 字符串，也能记录命令来源。

### 问：为什么命令必须使用 `shell=False`？

答：参数数组不会把路径、脚本名或任务文本重新解释为 shell 语法，能减少注入、转义和跨平台差异。脚本本身仍可能启动 shell，因此更严格隔离要留到沙箱阶段。

### 问：为什么需要同时记录 stdout、stderr 和 exit code？

答：很多测试框架把摘要写到 stdout，把 traceback 写到 stderr；只保存一个字符串会丢失来源。退出码也能区分通过、失败和进程启动异常。

### 问：失败输出为什么不能简单取最后 N 行？

答：最后几行经常只有汇总，真正的断言、文件路径和 traceback 位于中间。错误关键词加上下文窗口能在固定预算内保留更有价值的证据。

### 问：M3 为什么还不做完整沙箱？

答：验证闭环先解决命令发现、结果表达和修复反馈；进程隔离、网络限制、敏感文件保护和 allowlist 会显著改变执行模型，属于独立的 M5 安全阶段。

### 问：如何防止代理无限修复？

答：同时设置总工具轮数和独立的修复次数上限；每次修复必须有新的验证结果，连续失败达到上限就停止并报告剩余问题，不能只依赖模型自律。

### 问：如何证明代理真的完成了验证闭环？

答：集成测试要记录工具调用顺序，断言存在“发现 → 读取 → 修改 → 失败验证 → 根据错误再次读取 → 修改 → 通过验证”，并检查最终结构化历史，而不是只检查最终自然语言回答。

## 10. M3 最终验收清单

- [x] 固定发现、执行、压缩和迭代修复的验收指标。
- [x] `VerificationCommand` 和 `VerificationResult` 模型完成并有状态测试。
- [x] Python 项目可发现 pytest，并按当前解释器生成命令。
- [x] TypeScript 项目可从 package scripts 发现 test/lint/build/typecheck。
- [x] 发现结果带稳定 ID、来源、工作目录和可用性。
- [x] 验证执行使用 argv 和 `shell=False`，不自动安装依赖。
- [x] 通过、失败、超时、缺少运行时和启动异常可区分。
- [x] 失败输出在固定行数和字节数内，并保留错误上下文。
- [x] 工具返回面向模型的文本和结构化验证数据。
- [x] 代理能在失败后重新搜索、读取、打补丁并再次验证。
- [x] 修复次数、工具轮数和命令超时都有硬上限。
- [x] Python 单元测试、TypeScript 发现测试和至少一个端到端集成测试通过。
- [x] README、`implementation-plan.md` 和本指南的状态一致。

## 11. 完成后的架构

```text
context.py + ranking.py + instructions.py
                ↓
       search_text / read_many_files
                ↓
             apply_patch
                ↓
verification discovery
  (pyproject.toml / package.json)
                ↓
      structured command runner
                ↓
      compressed failure summary
                ↓
      agent repair loop + report
```

M3 完成后，项目才具备“修改后自动获得可行动反馈”的基础。下一阶段 M4 可以在不改变验证领域模型的前提下，把工具调用、审批和验证历史持久化为 JSONL。
