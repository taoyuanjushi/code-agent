# coding-agent

一个使用 Python 实现的本地 AI 编程 CLI。项目目标是逐步建立类似 Codex 的工作流：理解仓库、按需检索代码、通过受控工具修改文件、运行验证命令，并输出可审计的变更与验证结果。

当前已完成 M1“安全编辑闭环”、M2“更强的项目理解”、M3“验证闭环”、M4“会话持久化”和 M5“沙箱和权限增强”。当前发布与打包版本为 `0.4.0`，下一阶段进入 M6“产品化体验”。

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

`--list-sessions`、`--replay` 和 `--approvals` 不要求 `OPENAI_API_KEY`，也不会调用模型、工具、子进程或输入函数。回放默认只输出摘要；仅 `--verbose` 展开事件 payload 和 artifact。`--resume` 会沿用持久化的安全配置，不接受 `--write` 等新任务覆盖项。

## 测试与打包

```powershell
python -m pytest
python -m pytest -m "not local_rg"
python -m pytest tests/test_m3_acceptance.py tests/test_agent_verification.py -q
python -m pytest tests/test_m5_integration.py tests/test_cli_security.py -q
python -m pip wheel . -w dist
```

`local_rg` 和 `docker` 是可选 smoke marker；常规测试不要求安装 `rg` 或 Docker，也不调用真实模型。版本 `0.4.0` 已通过 wheel 元数据、内容、隔离安装、包导入和控制台入口 smoke 验收。若构建环境无法访问包索引，并且已安装 `pyproject.toml` 要求的构建依赖，可使用 `python -m pip wheel . -w dist --no-deps --no-build-isolation` 进行离线构建。

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
docs/
  implementation-plan.md
  m1-learning-and-interview.md
  m2-implementation-guide.md
  m3-implementation-guide.md
  m4-implementation-guide.md
  m5-implementation-guide.md
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
| M6+ | 规划中 | 流式终端 UI、review/explain 模式和编辑器集成 |

详细设计见：

- `docs/implementation-plan.md`
- `docs/m2-implementation-guide.md`
- `docs/m3-implementation-guide.md`
- `docs/m4-implementation-guide.md`
- `docs/m5-implementation-guide.md`

## 当前限制

本项目仍处于早期阶段，不是完整的 Codex 替代品。当前主要缺口包括：

- 已完成本地 wheel 构建和隔离安装 smoke；尚未配置签名、SBOM 或 PyPI 发布流水线。
- 自动修复依赖模型根据结构化失败上下文选择下一步，尚无跨会话策略学习。
- `--verbose` 会展开持久化 payload 和 artifact；输出可能包含代码、diff 或命令日志，分享前仍需人工检查。
- Docker backend 仅支持本地 Linux image，不自动 pull，网络固定为 `none`，snapshot 中的修改不会回写 workspace；流式终端 UI 和 IDE 集成尚未实现。
