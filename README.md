# coding-agent

一个使用 Python 实现的本地 AI 编程 CLI。项目目标是逐步建立类似 Codex 的工作流：理解仓库、按需检索代码、通过受控工具修改文件、运行验证命令，并输出可审计的变更与验证结果。

当前已完成 M1“安全编辑闭环”、M2“更强的项目理解”和 M3“验证闭环”，下一阶段将实现 M4“会话持久化”。

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

## 环境要求

- Python 3.12+
- OpenAI API key
- 可选：ripgrep，用于加速文本搜索

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

在受控环境中自动批准补丁或命令：

```powershell
python -m coding_agent --write --auto-approve-edits --auto-approve-commands --max-fix-attempts 3 "修复失败测试"
```

限制初始上下文样本：

```powershell
python -m coding_agent --context-max-files 4 --context-max-bytes-per-file 6000 "定位支付模块"
```

## 测试与打包

```powershell
python -m pytest
python -m pytest -m "not local_rg"
python -m pytest tests/test_m3_acceptance.py tests/test_agent_verification.py -q
python -m pip wheel . -w dist
```

`local_rg` 是依赖本机 ripgrep 的可选 smoke test；常规测试不要求安装 `rg`，也不调用真实模型。

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
tests/
  test_<module>.py
  fixtures/
docs/
  implementation-plan.md
  m1-learning-and-interview.md
  m2-implementation-guide.md
  m3-implementation-guide.md
pyproject.toml
```

## 安全边界

- 文件路径必须解析在 workspace 内。
- 被忽略文件和二进制文件不会进入清单、文本搜索或读取结果。
- 默认 `read-only`；遗留的 `write_file` 调用会被拒绝。
- `apply_patch` 会校验目标路径和 hunk 上下文，并要求批准。
- `run_command` 会拦截部分明显修改文件的命令，但当前仍使用 shell 字符串执行。
- 不自动执行高风险 Git 操作，也不会自动安装依赖。

当前命令策略不是完整沙箱。更严格的 allowlist、进程隔离、敏感文件保护和审批审计属于后续阶段。

## 开发里程碑

| 阶段 | 状态 | 说明 |
| --- | --- | --- |
| M1 | 已完成 | diff-first 编辑、审批、Git 差异和 failing-test 集成流程 |
| M2 | 已完成 | 统一忽略、AGENTS.md、按需搜索/读取、相关性排序和中型仓库验收 |
| M3 | 已完成 | Python/TypeScript 命令发现、相关性排序、受控执行、输出压缩、结构化工具和失败后迭代修复 |
| M4+ | 规划中 | 会话恢复、安全沙箱、流式 UI 和编辑器集成 |

详细设计见：

- `docs/implementation-plan.md`
- `docs/m2-implementation-guide.md`
- `docs/m3-implementation-guide.md`

## 当前限制

本项目仍处于早期阶段，不是完整的 Codex 替代品。当前主要缺口包括：

- 验证闭环状态当前只保存在进程内，退出后不能恢复。
- 自动修复依赖模型根据结构化失败上下文选择下一步，尚无跨会话策略学习。
- 尚无 JSONL 会话持久化、恢复和审批审计。
- 尚无完整 shell 沙箱、流式终端 UI 或 IDE 集成。
