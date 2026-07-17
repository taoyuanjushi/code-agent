# AI 编程工具详细实现计划

## 1. 产品目标

目标是实现一个本地优先的 AI 编程 CLI 工具，核心体验类似 Codex：

1. 理解当前项目结构和用户任务。
2. 读取必要文件，而不是盲目猜测。
3. 制定小步执行策略。
4. 通过受控工具修改代码。
5. 运行测试、类型检查或构建命令验证结果。
6. 输出变更摘要、验证结果和剩余风险。

第一阶段先做 CLI。IDE 插件、Web UI、多代理协作和远程沙箱放到后续阶段。

## 2. 技术选型

### 2.1 语言和运行时

- Python 3.12+

理由：

- 适合快速实现本地 CLI、文件系统工具、测试集成和跨平台脚本。
- OpenAI 官方 Python SDK 支持 Responses API。
- Python 的 subprocess、pathlib、pytest 适合实现可测试的本地代理工具。
- 后续如需 IDE 插件或 Web UI，可以在 Python agent core 外再加前端或编辑器扩展。

当前主线使用 Python。

### 2.2 模型接口

- 默认使用 OpenAI Responses API。
- 默认模型通过 `CODING_AGENT_MODEL` 配置，初始化为 `gpt-5.5`。
- reasoning effort 通过 `CODING_AGENT_REASONING_EFFORT` 配置。

Responses API 是当前代理循环的基础，因为它天然支持多轮响应、工具调用和推理配置。后续如果需要 tracing、handoff、多 agent 和更完整的运行时抽象，再评估 OpenAI Agents SDK。

### 2.3 CLI 框架和依赖

- `argparse`：命令行参数解析。
- `python-dotenv`：加载 `.env`。
- `pathlib`：跨平台路径处理。
- `subprocess`：受控命令执行。
- `pytest`：单元测试。

## 3. 当前初始化范围

本次初始化已包含：

- `pyproject.toml`、`.gitignore`、`.env.example`
- CLI 入口
- 配置加载
- workspace 文件扫描
- 系统提示词和用户提示词
- Responses API 调用
- function calling 工具循环
- 基础工具：
  - `read_file`
  - `apply_patch`
  - `list_files`
  - `search_text`
  - `git_status`
  - `git_diff`
  - `run_command`
- `write_file` 已停用，代码编辑强制使用 unified diff
- workspace 路径逃逸防护
- 单元测试和 failing-test fixture 集成测试

## 4. 核心模块设计

### 4.1 CLI 层

文件：`src/coding_agent/cli.py`

职责：

- 解析用户任务和参数。
- 加载 `.env`。
- 检查 `OPENAI_API_KEY`。
- 构造 `AgentConfig`。
- 调用代理运行器。
- 处理顶层错误。

规划命令：

```bash
coding-agent "修复 bug"
coding-agent --write "实现功能"
coding-agent --workspace ../project "分析项目"
coding-agent --model gpt-5.5 "重构模块"
```

后续扩展子命令：

```bash
coding-agent run
coding-agent review
coding-agent explain
coding-agent init
coding-agent session list
coding-agent session replay
```

### 4.2 配置层

文件：`src/coding_agent/config.py`

职责：

- 合并 CLI 参数、环境变量和默认值。
- 校验枚举和数字参数。
- 输出统一的 `AgentConfig`。

后续配置来源：

- `.coding-agent/config.json`
- 用户全局配置：`~/.coding-agent/config.json`
- 项目策略文件：`AGENTS.md`
- 模型/provider 配置
- 工具权限策略

### 4.3 上下文层

文件：`src/coding_agent/context.py`

当前能力：

- 扫描 workspace 文件清单。
- 忽略 `node_modules`、`.git`、构建产物和二进制文件。
- 优先采样 README、package、源码和测试文件。
- 限制采样文件数和单文件字节数。

后续增强：

- 读取 `.gitignore`。
- 解析 `AGENTS.md` 或项目约束文档。
- 结合 Git diff，只关注相关改动。
- 语义检索和 symbol 索引。
- Tree-sitter 代码结构索引。
- 大仓库分层检索：文件名搜索、文本搜索、embedding 检索、按需读取。

### 4.4 代理循环

文件：`src/coding_agent/agent.py`

当前流程：

1. 创建 session，并在上下文收集前写入 `session.started`。
2. 收集 workspace snapshot，记录 `context.created`。
3. 构造提示词，在调用 Responses API 前后记录 `model.requested`、`model.responded` 和 checkpoint。
4. 规范化模型响应并执行工具批次。
5. 先持久化工具、验证结果和 checkpoint，再把批次结果传回模型。
6. 重复直到模型给出最终回答或达到 turn limit。
7. 将最终报告保存为 artifact，并记录 completed、failed 或 interrupted 终结事件。

后续增强：

- 流式输出。
- 结构化计划状态。
- 基于现有事件日志的中断恢复。
- resume、replay 和会话管理 CLI。
- 更清晰的 tool call UI。
- 更严格的输出协议。
- 失败重试和错误分类。

### 4.5 工具层

文件：`src/coding_agent/tools.py`

当前工具：

- `read_file(path, max_bytes)`
- `apply_patch(patch)`
- `list_files(path)`
- `search_text(pattern, path)`
- `git_status()`
- `git_diff()`
- `run_command(argv, cwd, timeout_ms)`

`write_file` 不再向模型暴露；遗留调用会被拒绝并提示使用 `apply_patch`。

重要原则：

- 所有文件操作必须限制在 workspace 内。
- 默认只读。
- 写入需要 `--write`。
- 命令执行默认人工审批。
- 明显修改文件的命令在只读模式下拒绝。

后续工具：

- `read_many_files(paths)`
- `git_show(ref)`
- `run_tests(command)`
- `format_files(paths)`
- `create_plan(items)`
- `update_plan(item, status)`

### 4.6 路径和权限

文件：`src/coding_agent/path_safety.py`

当前能力：

- 将模型请求的路径解析为绝对路径。
- 拒绝逃逸 workspace 的路径。
- 写入前创建父目录。

后续增强：

- Windows junction/symlink 真实路径检查。
- denylist：`.env`、密钥文件、SSH key、系统目录。
- workspace-root allowlist。
- 文件大小限制。
- 二进制文件拒绝策略。

## 5. 权限模型设计

计划支持四种模式：

### 5.1 `read-only`

默认模式。

允许：

- 读取文件
- 列目录
- 运行只读命令

拒绝：

- 写文件
- 修改依赖
- Git 修改操作
- 删除、移动、覆盖文件

### 5.2 `workspace-write`

当前 `--write` 对应模式。

允许：

- 写 workspace 内文件
- 运行用户批准的修改命令

仍需拒绝：

- workspace 外路径
- 危险 shell 组合
- 密钥读取和泄露
- 破坏性 Git 命令，除非用户明确要求

### 5.3 `approval-required`

计划模式。所有写操作和命令都需要审批，审批界面展示：

- 工具名
- 参数
- 影响文件
- diff
- 预计命令

### 5.4 `full-auto`

计划模式。只建议用于临时目录、CI 或隔离容器。

必须配套：

- 沙箱
- 审计日志
- 超时和资源限制
- 文件变更快照

## 6. Patch 工作流

当前编辑流程已经切换为 diff-first，`write_file` 已停用：

1. 模型提出 patch。
2. 工具解析 unified diff。
3. 校验目标文件存在、上下文匹配、路径安全。
4. 展示完整 diff 和文件变更摘要。
5. 用户批准，或在受控环境中使用 `--auto-approve-edits`。
6. 应用 patch。
7. 使用 `git_status`、`git_diff` 和验证命令检查结果。

持久化审批审计日志属于 M4 会话功能。

新增工具：

```ts
apply_patch({
  patch: string
})
```

需要测试：

- 正常 patch
- 上下文不匹配
- workspace 逃逸
- 删除文件
- 新增文件
- CRLF/LF
- 大文件限制

## 7. 结构化命令执行设计

当前 `run_command` 只接受结构化 `argv`，并以 `shell=False` 执行；旧 `command` 字符串已被拒绝。下一步是将 argv 纳入集中命令策略和受控 runner。

后续应分层：

### 7.1 安全命令 API

优先提供结构化工具，而不是让模型拼 shell：

```python
run_pytest({"args": ["tests"]})
run_git({"args": ["status", "--short"]})
run_ripgrep({"pattern": "TODO", "path": "coding_agent"})
```

### 7.2 命令策略

- allowlist：`git status`、`git diff`、`python -m pytest`
- approval：formatter、migration、dependency install
- deny：`rm -rf /`、系统目录操作、网络 exfiltration、密钥打印

### 7.3 隔离运行

可选方案：

- 本地子进程 + 权限策略
- Windows Job Object / Linux cgroup 限制
- Docker sandbox
- 远程 ephemeral sandbox

## 8. 会话和状态

后续目录：

```text
.coding-agent/
  sessions/
    2026-07-02T10-00-00Z.jsonl
  approvals.log
  index/
```

记录内容：

- 用户任务
- 配置
- 模型响应 id
- 工具调用
- 工具结果
- 文件 diff
- 验证命令
- 最终回答

价值：

- 可恢复
- 可审计
- 可复现
- 可做失败分析

## 9. 测试策略

### 9.1 单元测试

优先覆盖：

- 路径安全
- 配置解析
- 上下文扫描忽略规则
- 工具参数解析
- 命令策略判断
- patch 解析

### 9.2 集成测试

使用临时 workspace：

1. 初始化一个小项目。
2. mock OpenAI response。
3. 让 agent 读取文件。
4. 让 agent 写文件。
5. 让 agent 运行测试。
6. 校验最终文件和输出。

### 9.3 端到端测试

在 fixture 项目上跑真实模型，任务包括：

- 修复单测失败
- 增加一个小功能
- 重构函数
- 解释代码
- 代码审查

真实模型测试默认不进 CI，需要手动启用。

## 10. 里程碑

### M0：项目初始化

状态：已完成。

交付：

- Python CLI 骨架
- Responses API 代理循环
- 基础工具
- README
- 实施计划
- 最小测试

### M1：可靠的本地编辑器

状态：已完成，并通过本地自动化验收。

目标：

- 引入 `apply_patch`
- diff 展示和审批
- 更严格的写入策略
- `git diff` 集成
- 单元测试覆盖核心安全逻辑

验收结果：

- 已通过 `tests/test_integration.py` 在 fixture 项目中完成“失败测试 → 读取代码 → 应用 patch → 查看 Git diff → pytest 通过”的闭环。
- `write_file` 已停用，模型代码编辑强制通过 `apply_patch`；人工和自动审批模式都会展示完整 unified diff。
- workspace 逃逸、patch 上下文不匹配、新增、修改和删除测试均通过。

已交付：

- `src/coding_agent/patch.py`：解析 unified diff、校验 hunk 上下文、应用新增/修改/删除文件。
- `apply_patch` 工具：在 `--write` 模式下应用补丁，默认需要人工确认。
- `--auto-approve-edits`：允许自动应用补丁，适合测试或受控环境。
- `git_status` 和 `git_diff` 工具：用于查看修改后的工作区状态和差异。
- `tests/test_patch.py`：覆盖正常修改、新增、删除、上下文不匹配和 workspace 逃逸。
- `tests/test_tools.py`：覆盖 `write_file` 禁用和完整 diff 展示。
- `tests/test_integration.py`、`tests/fixtures/failing_project/`：覆盖修复 failing test 的完整 M1 工作流。

后续强化项（不阻塞 M1 验收）：

- rename/mode change/binary diff 支持。
- symlink、junction 和敏感文件 denylist。
- 持久化审批与 diff 审计日志。

### M2：更强的项目理解（已完成）

目标：

- `.gitignore` 支持
- `AGENTS.md` 支持
- `rg` 搜索工具
- 多文件按需读取
- 文件相关性排序

验收结果：

- 已通过统一忽略策略、嵌套指令、中型仓库搜索→读取流程和参数边界测试。
- 初始上下文固定为最多 6 个样本，内容总量不超过 64 KiB，目标源码不预先注入。
- 全量测试已通过；具体步骤和测试矩阵见 `docs/m2-implementation-guide.md`。

### M3：验证闭环（已完成）

目标：

- 从 `pyproject.toml` 和 `package.json` 自动识别 test/lint/typecheck/build 命令。
- 使用参数数组和受控执行器运行已发现的验证命令。
- 在固定字节数和行数预算内压缩失败输出。
- 记录结构化验证结果，并驱动“失败→读取→修改→重试”循环。

实施顺序和固定验收指标见 docs/m3-implementation-guide.md。
已完成验收契约、验证领域模型、Python/TypeScript 统一命令发现、稳定且可解释的相关性排序、使用 argv 和 `shell=False` 的受控执行、错误上下文压缩、结构化验证工具、验证历史和有次数上限的失败后迭代修复循环。
建议验收：对小型 Python/TypeScript 项目完成一次可审计的“修复测试”闭环。

### M4：会话持久化（已完成）

目标：

- JSONL session log
- resume
- replay
- approvals audit
- prompt 和工具调用可追踪

建议验收：

- 进程中断后可以恢复。
- 可以完整审计一次代码修改。

逐步实现、事件模型、安全恢复和测试矩阵见 `docs/m4-implementation-guide.md`。
M4 已完成事件/检查点模型、显式 codec、JSONL SessionStore、隐私与 artifact 策略、纯 reducer、agent 循环事件接入、集中工具策略、完整审批审计、中断工具对账、workspace guard、跨进程 resume、CLI 会话入口、严格只读 replay、审批查询、最终测试矩阵和 wheel 验收。模型请求按 at-least-once 语义记录，工具批次在继续调用模型前完整落盘；补丁、验证和通用命令的批准、拒绝、异常、参数绑定与执行结果均可追踪。CLI 支持 workspace 内 session 列表、稳定 `latest` 选择、resume、schema version 2 摘要 replay、`--verbose` artifact 展开，以及按 session/action/outcome 过滤审批。只读查询不创建目录或锁，不调用模型、工具、subprocess 或输入函数。最终验收结果为核心会话测试 63 项、恢复/回放/集成测试 24 项、全量测试 406 项全部通过，`compileall` 与 `git diff --check` 通过；版本 `0.3.0` 的 `coding_agent-0.3.0-py3-none-any.whl` 已完成内容、元数据、隔离安装、包导入和控制台入口 smoke 验证。

### M5：沙箱和权限增强（已完成）

已完成安全验收合同、版本化安全领域模型、敏感路径与 realpath/symlink/Windows 路径边界、结构化 argv、命令策略、受控 host runner、过滤 snapshot、可选 Docker backend、CLI/full-auto 门禁、工具与提示词、SessionStore/resume/replay 审计，以及安全和跨平台测试矩阵。

目标：

- 命令 allowlist/denylist
- secret denylist
- symlink 真实路径校验
- Docker sandbox 可选支持
- full-auto 模式只允许在沙箱内启用

建议验收：

- 安全测试覆盖常见路径逃逸、shell 注入、敏感文件读取。

逐步实现、领域模型、Docker 边界、full-auto 门禁和最终测试矩阵见 `docs/m5-implementation-guide.md`。
M5 已完成；要求 sandbox 的命令在 capability 检查失败时 fail closed，不会回退宿主机。最终验收为核心安全合同 81 项、M5 集成/CLI 11 项、默认矩阵 640 项和全量测试 640 项通过；`compileall`、wheel 内容/元数据、隔离目录导入、控制台入口与 `git diff --check` 均通过。版本 `0.4.0` 的 wheel 包含完整 `security/`，不包含 tests、`.env` 或 `.coding-agent/`。详细结果见 `docs/m5-implementation-guide.md`。

### M6：产品化体验

目标：

- 流式输出
- 更好的终端 UI
- 计划面板
- 代码 review 模式
- explain 模式
- VS Code extension 原型

建议验收：

- 常用任务可以稳定交互。
- 用户能明确看到 agent 正在做什么、改了什么、验证了什么。

## 11. 近期开发顺序

建议按以下顺序继续：

1. 完成 M1 安全编辑闭环。已完成。
2. 完成 M2 项目理解和中型仓库验收。已完成。
3. 完成 M3 验证命令发现、结构化结果和迭代修复。已完成。
4. 保持 Python 默认测试和可选 TypeScript/Node smoke test 分离。已完成。
5. 完成 M4 JSONL session、审批审计、workspace guard、resume、CLI 会话入口、严格只读 replay、审批查询、最终测试矩阵和 wheel 验收。已完成。
6. 完成 M5 敏感路径、realpath/symlink、结构化 argv、命令策略、受控 runner、过滤 snapshot、Docker backend、full-auto 门禁、session 审计和最终验收。已完成。

## 12. 已知风险

- 基于规则的命令策略仍需随新增工具和运行时维护 allowlist/denylist；未知命令默认要求 sandbox。
- Docker backend 依赖本地预加载的 Linux image，不自动 pull、不开放网络，sandbox 修改也不回写 workspace。
- 当前基于规则的相关性排序对复杂语义任务仍有限。
- Responses API 返回对象结构在 SDK 版本变化时可能需要调整。
- 真实模型调用成本和时延需要通过配置控制。
- Windows、macOS、Linux 的 shell 行为不同，需要跨平台测试。

## 13. 成功标准

这个项目达到可用状态时，应满足：

- 默认不会修改用户文件。
- 开启写入后，每次修改都有可审计 diff。
- 能自主定位文件、改代码、运行测试、修复失败。
- 错误时能解释失败点，而不是静默退出。
- 对大多数普通项目不需要手工复制上下文。
- 权限边界清楚，危险动作可控。

## 14. 官方资料参考

- OpenAI Latest Model Guide: https://developers.openai.com/api/docs/guides/latest-model
- OpenAI Models: https://developers.openai.com/api/docs/models
- OpenAI Responses API migration guide: https://developers.openai.com/api/docs/guides/migrate-to-responses
- OpenAI Function Calling: https://developers.openai.com/api/docs/guides/function-calling
- OpenAI Agents SDK: https://developers.openai.com/api/docs/guides/agents
- OpenAI SDK quickstart: https://developers.openai.com/api/docs/quickstart
