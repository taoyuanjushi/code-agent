# M2 阶段详细实现指南：更强的项目理解

## 1. 学习目标

完成本阶段后，你应当能够：

1. 解释为什么中型仓库不能把大量源码一次性放进模型上下文。
2. 实现统一的 `.gitignore`、默认忽略目录和二进制文件过滤策略。
3. 发现并应用根目录及子目录中的 `AGENTS.md` 指令。
4. 使用 `rg` 完成安全、快速、结构化的文本搜索，并在 `rg` 不可用时回退到 Python 实现。
5. 让模型通过“文件清单 → 搜索 → 批量读取 → 编辑”的方式按需获取上下文。
6. 使用可解释、可测试的规则为文件排序。
7. 用单元测试和中型仓库集成测试完成 M2 验收。

M2 只解决“找到什么、读什么”。自动识别 test/build/lint 命令、失败压缩和迭代修复属于 M3，不应提前混入本阶段。

## 2. 先修知识

开始前建议掌握：

- `pathlib.Path`、相对路径和路径规范化。
- `dataclass`、类型注解和模块拆分。
- `subprocess.run(..., shell=False)` 与命令返回码。
- JSON Lines 解析。
- `.gitignore` 的目录规则、通配符和 `!` 否定规则。
- `pytest`、`tmp_path`、`monkeypatch` 和 fake client。
- 当前代理循环：`context.py → prompts.py → model_client.py → tools.py → agent.py`。

先执行基线测试：

```powershell
.\.venv\Scripts\python.exe -m pytest -q --basetemp=.coding-agent\pytest-m2-baseline
```

当前基线应为 `17 passed`。以后每完成一个步骤都重新运行测试，避免一次积累过多问题。

## 3. 核心概念

### 3.1 检索优先，而不是上下文堆积

初始上下文只负责告诉模型“仓库里大致有什么”和“必须遵守什么”。具体源码应通过搜索和读取工具按需加载。这样可以减少 token、降低无关代码干扰，并让行为适用于更大的仓库。

### 3.2 单一忽略策略

M2 改造前，`context.py` 和 `search.py` 分别实现 `_is_ignored()`，容易出现“文件清单看不到，但搜索能搜到”的不一致。因此默认忽略、`.gitignore` 和二进制判断需要集中到一个模块。

### 3.3 指令具有目录作用域

根目录 `AGENTS.md` 作用于整个仓库；子目录中的 `AGENTS.md` 只作用于该目录树。处理 `src/api/routes.py` 时，应按“根目录 → `src/` → `src/api/`”顺序合并指令，越靠近目标文件的规则优先级越高。

### 3.4 相关性必须可解释

M2 不需要 embedding。文件名、路径词、搜索命中、源码与测试对应关系、项目入口文件等规则，已经能构成一个稳定且容易测试的排序器。

## 4. 为什么重要

M2 改造前的默认配置最多采样 `80 × 24,000` 字节，理论上可能向初始请求加入接近 1.92 MB 文件内容；同时，采样主要依赖固定优先级和文件排序，并不知道用户任务。仓库稍大后会产生三个问题：

- 上下文成本高，真正相关的代码反而不突出。
- 生成目录、依赖目录或忽略文件可能污染搜索结果。
- 模型容易根据少量随机样本猜测，而不是主动定位实现。

M2 的验收重点不是“工具数量增加”，而是模型能在初始源码很少的情况下，自己找到相关文件。

## 5. 关键术语表

| 术语 | 含义 |
| --- | --- |
| Initial context | 第一次模型请求携带的仓库信息 |
| On-demand reading | 模型先搜索，再读取指定文件 |
| Ignore policy | 默认忽略、`.gitignore` 和二进制过滤的统一规则 |
| Scoped instructions | 只对某个目录树生效的 `AGENTS.md` |
| Search hit | 搜索命中的路径、行号、列号和预览 |
| Relevance score | 文件与当前任务的相关性分数 |
| Deterministic ranking | 相同输入始终产生相同排序 |
| Fallback | 首选实现不可用时启用的兼容实现 |
| Context budget | 初始上下文允许使用的文件数和总字节数 |

## 6. 详细实现步骤

### 6.1 第一步：先定义验收指标

不要先改生产代码。先新增 `tests/test_context.py` 和后续的中型仓库测试，明确以下结果：

- `.gitignore` 中的文件不会进入清单、样本或搜索结果。
- 根目录 `AGENTS.md` 会进入系统指令；嵌套指令只作用于对应目录树。
- 一次工具调用可以读取多个文件，并受文件数、单文件和总字节限制。
- 有 `rg` 时使用 `rg`；没有时仍能返回相同结构的结果。
- 初始上下文不包含大量源码正文。
- 模型能够先搜索，再读取目标文件。

建议把 M2 端到端指标固定为：初始样本不超过 6 个、初始内容总量不超过 64 KiB、目标源码不在初始样本中、两轮工具调用内定位并读取目标文件。数值可以配置，但测试中必须固定。

当前验收合同位于 `tests/test_context.py` 和 `tests/test_m2_integration.py`。实现早期曾使用 `pytest.mark.xfail(strict=True)` 保留 M1 基线；随着上下文预算和搜索后读取闭环落地，这两个标记均已移除，相关测试现已进入全量回归。

### 6.2 第二步：集中忽略规则

新增 `src/coding_agent/ignore.py`，让 `context.py`、`search.py`、`list_files` 和后续读取工具共用它。推荐使用 `pathspec`，不要手写一个只支持 `*` 的伪 `.gitignore` 解析器。

在 `pyproject.toml` 的运行时依赖中加入：

```toml
"pathspec>=0.12.0,<1.0.0"
```

建议接口：

```python
@dataclass(frozen=True)
class IgnorePolicy:
    root: Path
    gitignore_files: tuple[Path, ...]

    def is_ignored(self, path: Path) -> bool: ...
    def is_binary(self, path: Path) -> bool: ...


def load_ignore_policy(workspace: str | Path) -> IgnorePolicy: ...
```

实现顺序：

1. 保留 `DEFAULT_IGNORES`，保证 `.git`、`.venv`、`node_modules` 等目录始终被排除。
2. 从根目录开始发现 `.gitignore`，并记录其所在目录。
3. 将规则相对于对应 `.gitignore` 所在目录求值。
4. 按根目录到子目录的顺序应用规则，后出现的匹配可覆盖之前结果。
5. 支持空行、注释、目录规则、glob 和 `!` 否定规则。
6. 使用路径组件判断默认忽略项，不要用字符串 `startswith()`。
7. 统一使用 POSIX 风格相对路径进行匹配，避免 Windows `\` 影响规则。

测试至少覆盖：精确文件、整个目录、`*.log`、`!keep.log`、嵌套 `.gitignore`、默认忽略目录和二进制后缀。

### 6.3 第三步：实现 `AGENTS.md` 指令解析

新增 `src/coding_agent/instructions.py`。不要再把 `AGENTS.md` 仅当作普通采样文件。

建议类型和接口：

```python
@dataclass(frozen=True)
class AgentInstruction:
    path: str
    directory: str
    content: str


def discover_agent_instructions(workspace: str | Path) -> list[AgentInstruction]: ...

def instructions_for_path(
    instructions: list[AgentInstruction],
    target_path: str,
) -> list[AgentInstruction]: ...
```

规则如下：

1. 根 `AGENTS.md` 在启动时读取并加入 system prompt。
2. 嵌套 `AGENTS.md` 的路径加入仓库清单，但不必一次注入全部正文。
3. 读取或编辑某个文件时，解析从根到目标父目录的指令链。
4. 输出顺序为“通用 → 具体”，提示模型在冲突时遵守更具体的指令。
5. 每个指令文件设置字节上限，例如 16 KiB，并明确标注截断。
6. `AGENTS.md` 自身不应因普通 `.gitignore` 规则而消失；但仍必须受 workspace 路径安全约束。

**实现状态：已完成。** `src/coding_agent/instructions.py` 负责发现、截断、格式化和按目标路径筛选指令；根指令进入 system prompt，嵌套指令仅在读取对应目录树文件时按“根 → 最具体目录”返回。`context.py` 保留可见的 `AGENTS.md` 清单项但不把正文当普通样本注入，`read_file` 同时遵守忽略规则、二进制限制和路径安全。对应验收位于 `tests/test_instructions.py` 与 `tests/test_context.py`。

在 `build_system_prompt()` 中增加独立的 `Repository instructions` 区块，不要把指令混在普通文件样本代码块里。

### 6.4 第四步：新增多文件按需读取

新增 `src/coding_agent/reader.py`，并在 `types.py` 中定义结构化结果：

```python
@dataclass(frozen=True)
class FileReadResult:
    path: str
    ok: bool
    content: str
    truncated: bool
    error: str | None = None
    instruction_paths: tuple[str, ...] = ()
```

核心接口：

```python
def read_many_files(
    workspace: str,
    paths: list[str],
    *,
    max_files: int = 20,
    max_bytes_per_file: int = 30_000,
    max_total_bytes: int = 120_000,
) -> list[FileReadResult]: ...
```

实现要求：

- 保留请求顺序，便于模型把源码和测试成对读取。
- 每个路径都调用 `resolve_inside_workspace()`。
- 单个文件失败不能让整批结果丢失；为该文件返回 `ok=False`。
- 拒绝目录、二进制文件和被忽略文件。
- 同时执行文件数、单文件和总字节限制。
- 总预算耗尽后，后续项返回明确错误，而不是静默消失。
- 输出时使用稳定的文件边界，如 `===== path =====`。
- 返回每个目标文件适用的 `AGENTS.md` 路径；同一指令正文只输出一次，避免重复 token。

在 `TOOL_DEFINITIONS` 中新增 `read_many_files`，保留现有 `read_file`，用于只需精读一个文件的场景。

**实现状态：已完成。** `src/coding_agent/reader.py` 现在按请求顺序返回逐文件结果，执行文件数、单文件和总字节三层预算，并把路径逃逸、缺失文件、目录、忽略文件、二进制文件和无效 UTF-8 转换为局部失败。`read_many_files` 工具使用稳定文件边界，每个结果携带适用的 `AGENTS.md` 路径，公共指令正文只格式化一次。对应验收位于 `tests/test_reader.py` 和 `tests/test_context.py`。

### 6.5 第五步：把搜索升级为 `rg` 优先

保留工具名 `search_text` 可以减少模型和测试改动，但内部应优先调用 `rg`。使用 `shutil.which("rg")` 探测可执行文件。

推荐命令参数数组：

```python
args = [
    rg_path,
    "--json",
    "--line-number",
    "--column",
    "--color", "never",
    "--hidden",
    "--fixed-strings",
    pattern,
    search_path,
]
```

必须使用：

```python
subprocess.run(args, cwd=root, shell=False, ...)
```

不要把用户输入拼成 shell 字符串。需要额外处理：

- 默认使用字面量搜索；增加 `regex: bool = False` 后才允许正则。
- 可增加 `glob: list[str]`，每项转换为独立的 `--glob` 参数。
- 使用排除 glob 保留项目的默认忽略目录。
- 解析 `rg --json` 的 `match` 事件，转换成现有 `SearchMatch`。
- 返回码 `0` 表示有结果，`1` 表示无结果，`2` 及以上视为错误。
- 达到全局 `max_results` 后停止收集，且限制 stdout 大小。
- `rg` 不存在时调用 Python fallback；fallback 必须使用统一 `IgnorePolicy`。

建议为结果增加 `engine: Literal["rg", "python"]` 元数据，便于调试，但保持对模型输出格式简洁。

**实现状态：已完成。** `src/coding_agent/search.py` 现在通过 `shutil.which("rg")` 优先探测并调用 ripgrep，始终使用参数数组和 `shell=False`，解析 `--json` 的逐行命中，并在返回后再次应用统一 `IgnorePolicy` 和 glob 规则。未安装 `rg` 或可执行文件在调用前消失时会切换到 Python fallback；两条路径均支持字面量/正则、大小写、glob、全局结果上限和稳定的 POSIX 相对路径。UTF-8 字节偏移会转换为字符列号，保证 Unicode 文本下两种引擎结构一致。专项验收位于 `tests/test_search.py`、`tests/test_context.py` 和 `tests/test_tools.py`。

### 6.6 第六步：实现文件相关性排序

新增 `src/coding_agent/ranking.py`。输入至少包含文件元数据、用户任务和搜索命中：

```python
@dataclass(frozen=True)
class RankedFile:
    path: str
    score: int
    reasons: tuple[str, ...]


def rank_files(
    files: list[WorkspaceFile],
    task: str,
    search_hits: list[SearchMatch] | None = None,
) -> list[RankedFile]: ...
```

推荐第一版权重：

| 条件 | 分数示例 |
| --- | ---: |
| basename 与任务中的文件名完全匹配 | +100 |
| 路径 token 与任务 token 匹配 | 每项 +25 |
| 文件中出现搜索命中 | 每次 +15，上限 +60 |
| `README.md`、`pyproject.toml` 等项目入口 | +20 |
| 与已命中源码对应的测试文件 | +30 |
| 超大文件 | -10 至 -30 |

任务分词时统一小写，并按空格、`/`、`_`、`-`、`.` 拆分。排序键必须固定为 `(-score, path)`。`reasons` 让失败测试可以指出“为什么排在前面”，也方便之后调整权重。

不要在 M2 引入 embedding。先验证规则排序是否足够，再决定后续是否需要语义检索。

**实现状态：已完成。** 新增的 `src/coding_agent/ranking.py` 提供 `RankedFile` 和 `rank_files()`，按固定权重计算任务中精确文件名、路径 token、搜索命中、项目入口、关联测试和大文件惩罚。搜索命中按文件累计并封顶，Python/JavaScript 常见测试命名均可与已命中源码关联；每项得分保留稳定的 `reasons`，最终严格按 `(-score, path)` 排序。专项验收位于 `tests/test_ranking.py`。本步骤只提供独立排序能力；将排序结果接入初始清单和内容采样属于第七步。

### 6.7 第七步：重构初始上下文

修改 `collect_workspace_snapshot()`，让它接收 `task`，并把“完整扫描”和“内容采样”分开：

```python
def collect_workspace_snapshot(
    workspace: str,
    task: str,
    *,
    max_inventory_files: int,
    max_sample_files: int,
    max_bytes_per_file: int,
    max_total_sample_bytes: int,
) -> WorkspaceSnapshot: ...
```

初始上下文建议只包含：

1. workspace 根路径。
2. 根 `AGENTS.md` 正文。
3. 经过排序和截断的文件清单。
4. `README.md`、`pyproject.toml`、`package.json` 等少量项目元数据。
5. 用户明确点名文件时，该文件的有限片段。
6. 一段明确提示：源码尚未全部加载，应使用搜索和读取工具。

不要再按字典序采样大量 `.py`、`.ts` 文件。可把默认值调整为 `max_sample_files=6`、`max_bytes_per_file=8_000`、`max_total_sample_bytes=64_000`。同时给文件清单设上限，超出时展示文件总数和已省略数量。

如果不想一次修改太多 CLI 参数，可以保留现有参数作为兼容入口，并在内部增加总预算常量；第二次提交再补充更清晰的配置项。

**实现状态：已完成。** `collect_workspace_snapshot()` 现在接收任务和四层清单/采样预算，完整扫描结果先经 `rank_files()` 排序，再按清单上限截断；`WorkspaceSnapshot` 会记录总文件数和省略数量。初始正文仅包含用户明确点名的文件以及根目录项目元数据，不再按源码后缀批量采样，`AGENTS.md` 继续通过独立指令链处理。单文件与总样本字节均被严格限制，格式化结果会提示模型使用 `search_text` 和读取工具。`agent.py` 已传入任务，兼容配置默认值调整为 6 个样本、每文件 8,000 字节，内部清单和总内容上限分别为 400 个文件与 64 KiB。对应验收位于 `tests/test_context.py`、`tests/test_instructions.py` 和 `tests/test_m2_integration.py`。

### 6.8 第八步：更新提示词和代理工作流

在 `prompts.py` 中加入项目理解流程：

```text
Project understanding workflow:
1. Inspect repository instructions and the ranked inventory.
2. Search for task terms, symbols, errors, and likely tests.
3. Read only the relevant files, preferably in one read_many_files call.
4. Before editing, ensure applicable nested AGENTS.md files were considered.
5. Do not infer implementation details from file names alone.
```

`agent.py` 应把任务传给 snapshot/ranking，并将根指令传入 system prompt。工具顺序不需要硬编码，但提示应鼓励模型：

```text
inventory → search_text → read_many_files → apply_patch → git_diff
```

注意：M2 不负责自动选择或运行测试命令，因此不要在这里加入复杂的验证规划器。

**实现状态：已完成。** `src/coding_agent/prompts.py` 新增 `PROJECT_UNDERSTANDING_WORKFLOW`，按“检查根指令和排序清单 → 搜索任务词、符号、错误与测试 → 批量读取相关文件 → 编辑前确认嵌套 `AGENTS.md` → 不凭文件名猜实现”的顺序指导模型。提示词同时给出 `ranked inventory -> search_text -> read_many_files -> apply_patch -> git_diff` 推荐链路，并明确它只是可按任务裁剪的指导，而不是硬编码状态机；在编辑或断言实现细节前仍必须取得文件正文证据。`agent.py` 已由第七步负责传递任务与根指令。本步骤没有加入自动测试命令规划。对应验收位于 `tests/test_agent.py` 和 `tests/test_m2_integration.py`。

### 6.9 第九步：补齐工具层参数校验

在 `tools.py` 中完成以下校验：

- `paths` 必须是非空字符串数组。
- `max_files`、`max_results`、各字节限制必须为正整数，并设置硬上限。
- `regex` 和 `case_sensitive` 必须是布尔值，不能直接用 `bool("false")`。
- `glob` 必须是字符串数组。
- 搜索路径和读取路径必须位于 workspace 内。
- 工具错误继续通过 `ToolResult(ok=False, output=...)` 返回，不让单次错误结束代理循环。

建议抽出 `_require_positive_int()`、`_require_bool()` 和 `_require_string_list()`，减少各工具自行转换造成的不一致。

**实现状态：已完成。** `src/coding_agent/tools.py` 已统一使用 `_require_positive_int()`、`_require_bool()`、`_require_string_list()` 和严格字符串参数校验，并让运行时校验与工具 JSON schema 保持一致。当前硬上限为：批量读取最多 100 个路径、单文件最多 1 MiB、批次总量最多 4 MiB、搜索最多 1,000 条结果、命令超时最多 300,000 ms；glob 列表最多 100 项。`read_file`、`list_files` 和 `search_text` 不再把错误类型的 `path` 静默替换为根目录，所有读取与搜索路径继续通过 workspace 边界检查。非法调用统一返回 `ToolResult(ok=False, output=...)`，不会中断后续工具调用。对应回归验收位于 `tests/test_tools.py`，并与 `tests/test_reader.py`、`tests/test_search.py` 联合验证。

### 6.10 第十步：建立完整测试矩阵

建议新增或扩展以下测试文件：

| 测试文件 | 重点 |
| --- | --- |
| `tests/test_ignore.py` | 默认忽略、glob、否定、嵌套规则 |
| `tests/test_instructions.py` | 根/嵌套指令发现、作用域、优先级、截断 |
| `tests/test_reader.py` | 顺序、单项失败、路径逃逸、文件数和总字节限制 |
| `tests/test_search.py` | `rg` 参数数组、JSON 解析、返回码、fallback、glob/regex |
| `tests/test_ranking.py` | 权重、理由、稳定排序、源码与测试关联 |
| `tests/test_context.py` | 初始样本预算、忽略规则、任务相关清单 |
| `tests/test_agent.py` | 根指令进入 prompt，模型按搜索后读取的顺序调用工具 |
| `tests/test_m2_integration.py` | 中型仓库自主定位目标文件 |

对 `rg` 测试不要依赖开发机一定安装了 `rg`。单元测试中 monkeypatch `shutil.which` 和 `subprocess.run`；另外保留一个标记为可选的本机 smoke test。

**实现状态：已完成。** 第十步要求的八个测试模块已形成完整矩阵：忽略规则、指令作用域、批量读取预算、`rg`/fallback 搜索、排序理由、初始上下文预算、代理工具顺序和中型仓库定位均有独立验收。`tests/test_reader.py` 新增文件数及总字节耗尽边界，`tests/test_search.py` 新增非法 JSON 处理和真实 ripgrep smoke test，`tests/test_agent.py` 新增“根指令注入 → `search_text` → `read_many_files`”顺序测试。`pyproject.toml` 注册了 `local_rg` marker；未安装 `rg` 时 smoke test 自动跳过，也可使用 `-m "not local_rg"` 排除本机测试。八个矩阵模块共 54 项通过，全量测试共 100 项通过。

### 6.11 第十一步：实现中型仓库集成验收

在测试中动态创建仓库，避免提交上百个 fixture 文件：

```python
for index in range(120):
    path = workspace / "src" / "generated" / f"module_{index}.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"VALUE = {index}\n", encoding="utf-8")
```

然后加入：

- `.gitignore`：忽略 `src/generated/` 和 `*.log`。
- 根 `AGENTS.md`：要求先搜索再读取。
- `src/payments/AGENTS.md`：规定 payments 模块测试命名。
- 目标源码：`src/payments/refund_service.py`。
- 对应测试：`tests/payments/test_refund_service.py`。
- 若干名称相近但内容无关的干扰文件。

fake client 的预期流程：

1. 检查初始请求中有目标路径或相关目录信息，但没有目标源码正文。
2. 第一次调用 `search_text` 搜索任务关键词或 symbol。
3. 收到命中后调用 `read_many_files`，同时读取实现、测试和嵌套 `AGENTS.md`。
4. 最终回答准确指出目标实现位置。

核心断言：被忽略的 120 个文件不进入清单；初始上下文低于预算；目标源码通过工具按需读取；模型没有靠预注入源码完成任务。

**实现状态：已完成。** `tests/test_m2_integration.py` 现在动态构造中型仓库，生成 120 个被 `.gitignore` 排除的文件，并加入日志文件、相似名称但无关内容的干扰源码与测试、根及嵌套 `AGENTS.md`、目标实现和对应测试。fake client 严格验证“初始清单/相关目录信息 → `search_text` → `read_many_files` → 最终定位”的两轮流程：初始上下文最多 6 个样本、内容和输入均不超过 64 KiB，目标源码正文不预注入；搜索结果排除生成目录与日志；批量读取同时获取实现、测试和嵌套指令；最终答案准确给出实现和测试路径。专项集成测试通过，全量 pytest 通过。

### 6.12 第十二步：按小提交完成实现

推荐提交顺序：

1. `Add M2 acceptance tests and context budgets`
2. `Centralize workspace ignore policy`
3. `Load scoped AGENTS instructions`
4. `Add bounded multi-file reading tool`
5. `Use ripgrep with Python search fallback`
6. `Rank workspace files for the current task`
7. `Reduce initial context and guide tool discovery`
8. `Add medium-repository M2 integration test`
9. `Document M2 project-understanding workflow`

每个提交都应保持测试通过，且不要顺手重构 patch、审批或命令执行逻辑。

## 7. 最小可运行示例

实现 `read_many_files` 后，可以先脱离模型验证核心行为：

```python
from coding_agent.reader import read_many_files

results = read_many_files(
    ".",
    ["src/coding_agent/context.py", "src/coding_agent/search.py", "missing.py"],
    max_files=5,
    max_bytes_per_file=4_000,
    max_total_bytes=8_000,
)

for result in results:
    print(result.path, result.ok, result.truncated, result.error)
```

预期：前两个文件成功；单文件过大时 `truncated=True`；`missing.py` 返回失败结果；整个批次不会抛弃已经成功读取的内容。

完成搜索后再运行：

```powershell
rg --version
.\.venv\Scripts\python.exe -m pytest tests\test_search.py tests\test_reader.py -q --basetemp=.coding-agent\pytest-m2-search
```

即使第一条命令失败，第二条中的 fallback 测试也必须通过。

## 8. 常见误区

1. **把 `.gitignore` 当成简单后缀过滤。** 它包含目录作用域、glob、否定和规则顺序。
2. **认为 `rg` 一定存在。** CLI 应提供 Python fallback，而不是启动即失败。
3. **使用 `shell=True` 拼接搜索词。** 这会引入命令注入和跨平台转义问题。
4. **批量读取遇到一个坏路径就整体失败。** 模型需要看到逐文件结果。
5. **只读取根 `AGENTS.md`。** 子目录可能有更具体的测试或命名规则。
6. **相关性分数没有理由。** 没有 `reasons` 时很难判断权重错误。
7. **文件清单无限增长。** 只减少源码样本、不限制清单，同样会浪费上下文。
8. **用 fake client 证明真实模型智能。** fake client 只能验证协议和工具闭环；最终仍需一次人工 smoke test。
9. **提前实现 M3。** 自动测试命令识别会扩大范围，降低 M2 的可验收性。

## 9. 调试与排错

### 搜索结果与文件清单不一致

打印同一路径经过 `IgnorePolicy.is_ignored()` 的结果，确认 `context.py` 和 `search.py` 没有保留各自的旧判断。重点检查 Windows 路径是否先转换为 `/`。

### `rg` 有输出但解析不到结果

保存一小段 `rg --json` 输出，确认只解析 `type == "match"` 的事件，并从 `data.path.text`、`line_number`、`submatches` 中取值。不要把 summary 或 begin/end 事件当成命中。

### 否定规则无效

检查规则应用顺序以及父目录是否已被直接剪枝。若在遍历时过早跳过整个目录，目录中的 `!keep.txt` 可能永远没有机会重新包含；应通过测试明确采用的语义。

### 初始上下文仍然过大

分别记录 inventory 字节数、instructions 字节数和 samples 字节数。不要只统计文件正文。测试最终格式化后的字符串长度，而不只是原始文件大小。

### 排序结果偶尔变化

检查是否使用了 `set` 的迭代顺序，最终排序是否包含路径作为 tie-breaker，以及任务 token 是否经过统一规范化。

### 测试产生临时目录冲突

在本仓库中使用 workspace 内的 pytest 临时目录：

```powershell
.\.venv\Scripts\python.exe -m pytest -q --basetemp=.coding-agent\pytest-m2-final
```

## 10. 面试会怎么问

### 问：为什么不把整个仓库直接放进模型上下文？

答：仓库越大，无关信息越多，token 成本和延迟越高，相关代码反而更难突出。更稳定的方式是先提供受限清单和项目指令，再通过文本搜索与多文件读取按需加载证据。

### 问：为什么 `.gitignore` 逻辑必须集中管理？

答：文件清单、搜索和读取如果使用不同规则，会产生可见性不一致。统一的 `IgnorePolicy` 可以确保同一文件在所有工具中的判断一致，也便于单元测试覆盖。

### 问：调用 `rg` 时为什么使用参数数组和 `shell=False`？

答：参数数组避免用户搜索词被解释为 shell 语法，减少注入风险和引号转义问题，同时提高 Windows、Linux 和 macOS 的一致性。

### 问：为什么还需要 Python fallback？

答：`rg` 是外部可执行文件，用户环境可能没有安装。fallback 保证核心功能可用；`rg` 提供性能，Python 实现提供可移植性，两者返回统一的数据结构。

### 问：嵌套 `AGENTS.md` 应如何处理冲突？

答：先应用根目录的通用规则，再应用目标路径父目录链上的更具体规则；越靠近目标文件的规则优先。实现上保留来源路径和顺序，让模型知道每条规则的作用域。

### 问：如何证明“按需读取”真的生效？

答：集成测试应断言目标源码正文不在初始请求中，模型先调用搜索，再根据命中调用多文件读取，并在最终结果中正确定位文件。同时对初始上下文设置明确字节预算。

### 问：规则排序相比 embedding 有什么优点？

答：规则排序实现成本低、结果确定、原因可解释、测试简单，适合 M2。缺点是无法很好理解同义词和复杂语义；当规则召回不足时再评估 embedding，而不是一开始增加索引和存储复杂度。

### 问：批量读取为什么要返回部分成功？

答：代理请求的路径可能包含过期文件名、目录或二进制文件。逐项返回结果可以保留已读取的有效上下文，并让模型针对失败项调整搜索，而不是重做整个请求。

## 11. 本章小结

M2 的核心不是增加五个孤立功能，而是建立统一的项目理解链路：

```text
统一忽略策略
    ↓
受限且相关的初始清单 + AGENTS.md
    ↓
rg/Python 搜索
    ↓
多文件按需读取
    ↓
可解释的文件排序
```

完成后，代理应从“启动时随机读一批源码”转变为“先观察、再搜索、最后读取证据”。只有中型仓库集成测试满足上下文预算和自主定位断言，才应把 M2 标记为完成。

## 12. 延伸阅读与下一步建议

实现过程中重点阅读本仓库以下文件：

- `src/coding_agent/context.py`：当前扫描和静态采样逻辑。
- `src/coding_agent/search.py`：现有 Python 字面量搜索。
- `src/coding_agent/tools.py`：工具 schema、参数解析和错误包装。
- `src/coding_agent/prompts.py`：模型工作流约束。
- `src/coding_agent/agent.py`：初始上下文进入代理循环的位置。
- `tests/test_agent.py`：fake client 的工具调用测试方式。
- `tests/test_integration.py`：M1 端到端 fixture 设计参考。

最终验收清单：

- [x] 所有扫描、搜索和读取共用忽略策略。
- [x] `.gitignore` 的目录、glob、否定和嵌套规则有测试。
- [x] 根及嵌套 `AGENTS.md` 的作用域与优先级明确。
- [x] `read_many_files` 有文件数、单文件和总字节限制。
- [x] 工具参数拒绝隐式类型转换、空路径数组和超过硬上限的请求。
- [x] `rg` 使用 `shell=False`，并有 Python fallback。
- [x] 文件排序稳定、可解释并与任务相关。
- [x] 系统提示明确引导“清单 → 搜索 → 读取”，并在编辑前检查嵌套指令。
- [x] 初始上下文有总预算，不批量注入源码。
- [x] 中型仓库测试能完成“搜索 → 读取 → 定位”。
- [x] 中型仓库验收覆盖 120 个忽略文件、相似干扰文件和两轮搜索 → 读取流程。
- [x] 八个 M2 核心测试模块形成完整矩阵，并保留可选本机 ripgrep smoke test。
- [x] 全量 pytest 通过。
- [x] `README.md` 和 `implementation-plan.md` 在验收后更新状态。

M2 完成后再进入 M3：自动识别验证命令、压缩失败输出、记录结构化验证结果，并让代理根据失败继续迭代。
