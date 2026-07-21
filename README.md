# coding-agent

一个使用 Python 实现的本地 AI 编程 CLI。项目目标是逐步建立类似 Codex 的工作流：理解仓库、按需检索代码、通过受控工具修改文件、运行验证命令，并输出可审计的变更与验证结果。

当前已完成 M1“安全编辑闭环”、M2“更强的项目理解”、M3“验证闭环”、M4“会话持久化”、M5“沙箱和权限增强”和 M6“产品化体验”。当前发布与打包版本为 `0.5.0`；M6 的 UI event、streaming、稳定 live JSONL、持久化计划、run/review/explain、恢复/回放、VS Code prototype 以及产品与跨平台验收均已完成。

## 已实现能力

- Python 3.12+ CLI 和 OpenAI Responses API 代理循环。
- 默认模型为 `gpt-5.5`，默认只读；使用 `--write` 后才允许修改 workspace。
- 所有代码编辑必须通过 unified diff `apply_patch`，应用前展示完整 diff。
- 根目录和嵌套 `AGENTS.md` 指令按目录作用域生效。
- `.gitignore`、默认忽略目录和二进制过滤由统一策略处理。
- 初始上下文使用受限文件清单和相关性样本，不批量注入源码。
- 模型可通过 `search_text` 和 `read_many_files` 先搜索、再按需读取。
- 文本搜索优先使用 `rg`，不可用时自动回退到 Python 实现。
- 验证命令会从 Python/TypeScript 配置中发现，按任务相关性稳定排序，并通过受控 argv 执行。
- 失败输出会在行数和字节预算内保留错误上下文；代理可据此重新搜索、修复并重跑失败命令。
- 命令执行默认要求交互确认，修改结果可通过 `git_status` 和 `git_diff` 检查。
- 已具备 M4 事件模型、JSONL `SessionStore`、哈希链校验、敏感值过滤和大字段 artifact 化；代理循环会记录上下文、模型请求/响应、工具结果、检查点和终结事件。
- `resume_agent()` 会校验 workspace、Git HEAD、已触碰文件哈希和单 writer 租约，按持久化 phase 继续执行，并避免重复运行已完成工具。
- CLI 支持 `--resume`、`--replay`、`--list-sessions`、`--approvals` 和稳定 JSON 输出；只读查询不创建锁或目录，`latest` 仅在当前 workspace 中按事件时间稳定选择。
- 命令策略会 hard-deny 破坏性操作，将未知命令、shell、联网和内联解释器路由到隔离后端；受控 host runner 使用过滤环境、输出预算、超时和进程树终止。
- 可选 Docker backend 使用过滤后的临时 workspace snapshot、固定 image digest、无网络、只读 rootfs、capability drop 和资源限制；要求 sandbox 的命令不会回退到 host。
- `--full-auto` 仅在本地 Linux Docker image 可用且 digest 已固定时启动，resume/replay 会保留并审计安全决策、sandbox 事件和镜像漂移。
- new/resume 模型输出默认通过 Responses API streaming 增量显示；`--no-stream` 可切回完整响应模式，二者保持相同的 normalized response 和 session 事实语义。
- new/resume 支持 `--output human|jsonl`；live JSONL 把 delta、工具、审批、验证和终态事件逐行写入 stdout，交互审批提示独立写入 stderr。human 输出可用 `--no-color` 显式禁色。
- `update_plan` 以 session-only 工具维护完整计划，不写 workspace、无需审批；最新不可变计划通过 durable `plan.updated` 进入 checkpoint、resume、replay 和 live JSONL。terminal 使用 `[ ]`、`[>]`、`[x]` 显示进度，全部完成后不能重新打开。
- `--mode run|review|explain` 使用持久化的显式工具 profile；受限 mode 在 schema、dispatch、审批和副作用之前硬拒绝写入与通用进程工具，resume 沿用原 mode。
- review mode 通过唯一一次 session-only `submit_review` 返回结构化 findings；提交会验证 severity、预算、非敏感 workspace 文本路径和有效行号，结果可从 checkpoint、resume、replay、human terminal 和 live JSONL 恢复或消费。
- explain mode 复用只读 search/read/Git 工具和现有文本答案；成功读取会把实际可见最大行号作为 durable evidence，最终 `path:line` 引用只能指向已读取文件且不能越界。没有证据时必须明确说明不足，UI 默认隐藏 reasoning summary，不新增 explanation AST。
- 顶层退出体验已统一：成功与 stdout pipe 提前关闭返回 `0`，agent/runtime 与 policy/sandbox preflight 失败返回 `1`，usage/config 错误返回 `2`，Ctrl+C 返回 `130`。中断会先清理活动进程树并持久化 `session.interrupted`；resume preflight 失败会把既有 session 终结为 `session.failed`。
- `editors/vscode/` 提供无 runtime npm 依赖的最小 VS Code prototype：三个命令通过 `ProcessExecution(executable, args)` 在专用 task terminal 启动现有 CLI，多根 workspace 显式选择 folder，explain 只传 active file 的 workspace-relative path。

## 环境要求

- Python 3.12+
- OpenAI API key
- 可选：ripgrep，用于加速文本搜索
- 可选：Docker，用于隔离命令和 `--full-auto`

## 安装

Windows PowerShell：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
```

如果本机镜像缺少依赖，可临时使用官方 PyPI：

```powershell
python -m pip install -i https://pypi.org/simple -e ".[dev]"
```

在 `.env` 中填写：

```bash
OPENAI_API_KEY=your_api_key
CODING_AGENT_MODEL=gpt-5.5
CODING_AGENT_REASONING_EFFORT=medium
CODING_AGENT_SANDBOX=auto
CODING_AGENT_SANDBOX_IMAGE=python:3.12-slim
```

也可以只为当前 PowerShell 会话设置密钥：

```powershell
$env:OPENAI_API_KEY="your_api_key"
```

## 使用方式

只读分析：

```powershell
python -m coding_agent "分析项目结构并指出可改进之处"
```

新任务默认使用 `run` task mode。需要只读审查当前改动或基于仓库证据解释代码时，可显式选择 `review` 或 `explain`：

```powershell
python -m coding_agent --mode review "审查当前 Git diff"
python -m coding_agent --mode explain "解释会话恢复流程并引用 path:line"
```

`review`/`explain` 只暴露 read/search、固定只读 Git 和 `update_plan`，其中 review 额外暴露无需审批的 session-only `submit_review`。两者都不能与 `--write`、自动批准或 `--full-auto` 组合；task mode 会持久化到 session，`--resume` 沿用原 mode 且不接受覆盖。review 必须恰好成功提交一次结构化结果；没有问题时提交非空 summary 和空 `findings`。

explain 会先 search/read，再以现有 `AgentRunReport.answer` 文本说明结论。文件依据应写成反引号包围的 `` `path:line` ``；只有成功 `read_file` / `read_many_files` 且已经写入 session 的 workspace 文件可以被引用，行号不能超过模型实际看到的内容。resume 会复用此前的 durable read evidence；如果没有足够证据，可以不伪造引用，但必须明确说明无法确认。explain 的 live/human UI 不显示 reasoning summary。

模型正文默认流式显示；需要兼容完整响应或排查流式连接时使用 `--no-stream`：

```powershell
python -m coding_agent --no-stream "分析项目结构"
```

需要供日志、管道或编辑器子进程稳定消费时，使用 live JSONL；默认仍保留 streaming，只有显式 `--no-stream` 才关闭：

```powershell
python -m coding_agent --output jsonl "分析项目结构"
python -m coding_agent --resume latest --output jsonl --no-stream
```

JSONL 模式的 stdout 每行都是一个带 `schema_version`、连续 `seq`、`type` 和 `payload` 的 UI event；审批 request/decision 也在 stdout，输入提示写 stderr，回答继续从 stdin 读取。人类输出可通过 `--no-color` 禁用 ANSI 颜色。

review 任务完成时，`run.finished.payload.review` 包含 `summary` 和结构化 `findings`；每条 finding 包含 `severity`、workspace-relative `path`、有效 `line`、`title` 和 `detail`。TTY/human 输出按 severity、path、line、title 稳定排序，JSONL 不从模型自由文本重建 findings。

允许修改 workspace：

```powershell
python -m coding_agent --write "修复计算逻辑并运行测试"
```

使用本地已存在且可固定 digest 的 Docker image 启用 full-auto：

```powershell
python -m coding_agent --full-auto --sandbox-image python:3.12-slim --max-fix-attempts 3 "修复失败测试"
```

默认 `--sandbox auto`；也可显式使用 `--sandbox none` 或 `--sandbox docker`。`--full-auto` 和 `--auto-approve-commands` 要求 Docker capability 与本地 image digest 在模型启动前验证成功，不会自动 pull，也不会回退 host。

限制初始上下文样本：

```powershell
python -m coding_agent --context-max-files 4 --context-max-bytes-per-file 6000 "定位支付模块"
```

会话命令：

```powershell
python -m coding_agent --list-sessions
python -m coding_agent --resume latest
python -m coding_agent --replay latest
python -m coding_agent --replay latest --verbose
python -m coding_agent --replay 20260714T031500Z-a1b2c3d4 --json
python -m coding_agent --approvals
python -m coding_agent --approvals latest --approval-action apply_patch --approval-outcome approved --json
```

`--list-sessions`、`--replay` 和 `--approvals` 不要求 `OPENAI_API_KEY`，也不会调用模型、工具、子进程或输入函数。回放默认只输出摘要；human replay 会展示 durable terminal 状态、验证状态、完整 plan 更新历史和结构化 review findings，仅 `--verbose` 展开事件 payload 和 artifact。查询命令的 `--json` 始终输出单个 JSON document；replay schema 以兼容新增字段的方式提供 `plan_updates` 与 `terminal`，不会记录仅属于 UI 的 model delta。new/resume 的 `--output jsonl` 输出 live JSONL event，两类选项不会混用。`--resume` 会沿用持久化的 task mode、安全配置和计划，banner 显示 permission、sandbox、上次 phase/status 与计划进度，不接受 `--write` 等新任务覆盖项。

退出码合同：

| 情况 | 退出码 | durable 结果 |
| --- | ---: | --- |
| 正常完成 | `0` | `session.completed` |
| usage/config 错误 | `2` | 不启动 session |
| policy/sandbox preflight 失败 | `1` | 不调用模型；resume 已有 session 时记录 `session.failed` |
| agent/runtime 失败 | `1` | `session.failed` |
| Ctrl+C | `130` | 清理活动进程后记录 `session.interrupted` |
| stdout pipe 关闭 | `0` | 不输出 traceback，保留已经发生的 session facts |

未完成的 streaming response 不会伪造 response ID。resume 会沿用 `at_least_once_after_unrecorded_response` 语义追加带 `retry_of_seq` 的新请求；renderer 关闭不会重新执行工具。

## VS Code prototype

`editors/vscode/` 是 M6 的最小本地扩展，不包含 bundler、framework、daemon、webview 或 npm runtime dependency。使用方式：

1. 安装本项目，确保 `coding-agent` 在 `PATH`；若不在，设置 `codingAgent.executable` 为可执行文件路径。
2. 在 VS Code 中打开 `editors/vscode/`，按 `F5` 启动 `Run Coding Agent Extension`。
3. 在 Extension Development Host 中打开目标项目，通过命令面板运行 `Coding Agent: Run Task`、`Coding Agent: Review Changes` 或 `Coding Agent: Explain Current File`。

扩展使用 `vscode.ProcessExecution` 的 executable + args 数组启动 CLI，不构造 shell command。Run Task 传入 `--write`；Review Changes 使用 `--mode review`；Explain Current File 使用 `--mode explain`，只把当前文件的 workspace-relative path 放入任务文本，不传编辑器选区正文。多根 workspace 会先显示 folder picker。三个命令都在专用 VS Code task terminal 中运行，因此审批输入、流式颜色输出、Ctrl+C 和 CLI 退出码保持原有行为。更详细的开发说明见 `editors/vscode/README.md`。

## 测试与打包

```powershell
python -m pytest
python -m pytest -m "not local_rg"
python -m pytest tests/test_m3_acceptance.py tests/test_agent_verification.py -q
python -m pytest tests/test_m5_integration.py tests/test_cli_security.py -q
python -m pytest tests/test_m6_acceptance.py tests/test_m6_step14.py tests/test_m6_step15.py tests/test_m6_integration.py tests/test_cli_product.py -q
python -m pytest -m "not docker and not live_model and not vscode" -q
node .\editors\vscode\test\argv.test.js
python -m pip wheel . -w dist
```

`local_rg`、`local_node`、`docker`、`live_model` 和 `vscode` 用于本地或 opt-in smoke；仓库级 VS Code 静态测试不要求安装 VS Code，Node helper smoke 在未安装 Node 时跳过。常规测试不要求 `rg`、Docker、VS Code 或真实模型。版本 `0.5.0` 已通过 wheel 元数据、内容、隔离安装、包导入和控制台入口 smoke 验收；真实模型与 VS Code Development Host 仍是 opt-in，不阻塞默认离线验收。若构建环境无法访问包索引，并且已安装 `pyproject.toml` 要求的构建依赖，可使用 `python -m pip wheel . -w dist --no-deps --no-build-isolation` 进行离线构建。

## 项目结构

```text
src/
  coding_agent/
    cli.py           CLI 参数、环境变量和退出码
    config.py        运行配置加载与校验
    agent.py         Responses API 工具调用循环
    model_client.py  模型客户端协议和 OpenAI 实现
    prompts.py       系统提示、任务提示和工具工作流
    context.py       受预算约束的仓库清单与初始样本
    ignore.py        .gitignore、默认忽略和二进制策略
    instructions.py  根目录及嵌套 AGENTS.md 解析
    ranking.py       任务相关文件的稳定、可解释排序
    reader.py        受文件数和字节预算限制的批量读取
    search.py        rg 优先、Python fallback 的文本搜索
    verification.py 验证命令发现、排序、安全执行与输出压缩
    tools.py         工具 schema、参数校验与执行入口
    plans.py         不可变计划模型、预算和完整计划校验
    reviews.py       结构化 review 模型、预算、去重和排序
    explanations.py  explain 读取证据、path:line 提取与最终校验
    task_modes.py    run/review/explain 工具 profile 与提示片段
    patch.py         unified diff 解析、校验和应用
    path_safety.py   workspace 路径安全校验
    types.py         共享数据类型
    security/        命令/路径策略、受控进程、snapshot 和 Docker backend
    sessions/
      models.py      会话事件、检查点、审批和 artifact 领域模型
      codec.py       规范化 JSON、事件哈希和严格编解码
      privacy.py     配置白名单、敏感值过滤和大字段 artifact 化
      reducer.py     从事件确定性重建不可变会话状态
      store.py       JSONL、单 writer 锁、尾部修复和原子 artifact I/O
      recovery.py    中断工具对账、去重和恢复计划
      workspace_guard.py  workspace、Git HEAD 和文件哈希校验
      query.py       workspace 内 session 选择与列表投影
      replay.py      严格只读回放、artifact 展开与审批查询
tests/
  test_<module>.py
  fixtures/
editors/
  vscode/             plain JavaScript 最小扩展、argv helper 与 Development Host 配置
docs/
  implementation-plan.md
  m1-learning-and-interview.md
  m2-implementation-guide.md
  m3-implementation-guide.md
  m4-implementation-guide.md
  m5-implementation-guide.md
  m6-implementation-guide.md
pyproject.toml
```

## 安全边界

- 文件路径必须解析在 workspace 内。
- 被忽略文件和二进制文件不会进入清单、文本搜索或读取结果。
- 默认 `read-only`；遗留的 `write_file` 调用会被拒绝。
- `apply_patch` 会校验目标路径和 hunk 上下文，并要求批准。
- `run_command` 只接受结构化 `argv`，以 `shell=False` 执行，并会保留 argv、cwd 和超时信息的审批与 session 审计。
- host 仅执行策略明确允许且已完成所需审批的命令；未知命令、shell、内联解释器、联网和安装操作要求 Docker，hard deny 永不执行。
- Docker 仅挂载过滤后的临时 snapshot，固定 `--network none`、只读 rootfs、capability drop、进程/内存/CPU 限制；sandbox 内修改不会回写真实 workspace。
- `.env`、私钥、云凭据、包管理器凭据和 `.coding-agent/` 默认禁止读取、搜索、列出、artifact 展开和 snapshot；`.env.example`、`.env.sample` 可作为安全示例。
- `--full-auto` 和命令自动批准会在模型启动前验证 Docker capability、本地 image 和固定 digest，失败即退出且不回退 host。
- 不自动执行高风险 Git 操作，也不会自动安装依赖。
- Session 持久化会精确替换当前进程中已知的 API key、token、secret 和 password 值，并拒绝记录完整环境变量或认证头。
- `.coding-agent/` 仍可能包含任务描述、代码片段、diff 和命令输出；不要直接上传、提交或公开分享该目录。
- resume 会拒绝不同 workspace、Git HEAD 漂移、无法解释的已触碰文件变化和并发 writer；未知结果的进程工具必须重新批准。
- 敏感值过滤不能发现源码中的所有秘密，尤其不能替代 secret scanning、访问控制和人工检查。

CLI 已接入 Docker capability 选择、image digest 固定和 full-auto 前置门禁。`run_command` 与 `run_verification` 已接入统一的 host/Docker 安全路由和 `secure_command_result`；要求 Docker 的命令 fail closed，不会回退到宿主机。

## 开发里程碑

| 阶段 | 状态 | 说明 |
| --- | --- | --- |
| M1 | 已完成 | diff-first 编辑、审批、Git 差异和 failing-test 集成流程 |
| M2 | 已完成 | 统一忽略、AGENTS.md、按需搜索/读取、相关性排序和中型仓库验收 |
| M3 | 已完成 | Python/TypeScript 命令发现、相关性排序、受控执行、输出压缩、结构化工具和失败后迭代修复 |
| M4 | 已完成 | 会话模型、SessionStore、隐私与 artifact 策略、纯 reducer、事件与审批审计、中断工具对账、workspace guard、resume、严格只读 replay、审批查询、最终测试矩阵和 wheel 验收 |
| M5 | 已完成 | 结构化 argv、命令/敏感路径策略、symlink 防护、受控 runner、过滤 snapshot、Docker backend、full-auto 门禁、session resume/replay、安全矩阵和 wheel 验收 |
| M6 | 已完成 | UI event、terminal/JSONL、模型 streaming、工具/审批/恢复状态、稳定 live JSONL、持久化计划、run/review/explain、结构化 review、只读 explain、中断/resume/replay/退出码、最小 VS Code prototype、产品与跨平台矩阵及最终 wheel 验收 |

详细设计见：

- `docs/implementation-plan.md`
- `docs/m2-implementation-guide.md`
- `docs/m3-implementation-guide.md`
- `docs/m4-implementation-guide.md`
- `docs/m5-implementation-guide.md`
- `docs/m6-implementation-guide.md`

## 当前限制

本项目仍处于早期阶段，不是完整的 Codex 替代品。当前主要缺口包括：

- 已完成本地 wheel 构建和隔离安装 smoke；尚未配置签名、SBOM 或 PyPI 发布流水线。
- 自动修复依赖模型根据结构化失败上下文选择下一步，尚无跨会话策略学习。
- `--verbose` 会展开持久化 payload 和 artifact；输出可能包含代码、diff 或命令日志，分享前仍需人工检查。
- Docker backend 仅支持本地 Linux image，不自动 pull，网络固定为 `none`，snapshot 中的修改不会回写 workspace。VS Code 当前仅提供可从 Extension Development Host 加载的本地 prototype，尚无 Marketplace 打包/发布、自定义 webview、后台服务或真实跨平台 VS Code smoke。
