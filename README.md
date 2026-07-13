# coding-agent

一个本地 AI 编程 CLI 工具骨架，目标是逐步实现类似 Codex 的工作流：读取项目上下文、调用模型推理、使用受控工具读取/修改文件、运行验证命令，并给出清晰的执行结果。

当前主实现使用 Python。

- Python 3.12+ CLI
- OpenAI Responses API
- 默认模型：`gpt-5.5`
- 默认只读模式
- `--write` 后才允许写入 workspace
- 代码编辑强制通过 unified diff `apply_patch`，应用前始终展示完整 diff
- 命令执行默认需要交互确认
- 初始上下文自动扫描仓库文件列表和关键源码文件

## 环境要求

- Python 3.12+
- OpenAI API key

## 安装

Windows 上建议明确使用 Python 3.12：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

如果本机 pip 镜像源找不到依赖包，可以临时使用官方 PyPI：

```powershell
python -m pip install -i https://pypi.org/simple -e ".[dev]"
```

复制环境变量模板：

```powershell
Copy-Item .env.example .env
```

然后填写：

```bash
OPENAI_API_KEY=your_api_key
CODING_AGENT_MODEL=gpt-5.5
CODING_AGENT_REASONING_EFFORT=medium
```

Windows PowerShell 也可以直接设置当前会话变量：

```powershell
$env:OPENAI_API_KEY="your_api_key"
```

## 使用

只读分析：

```powershell
python -m coding_agent "分析这个项目的结构并指出可以改进的地方"
```

允许写文件：

```powershell
python -m coding_agent --write "给这个项目增加一个 README 示例"
```

允许自动应用补丁：

```powershell
python -m coding_agent --write --auto-approve-edits "修复一个 Python 类型错误"
```

允许自动执行命令：

```powershell
python -m coding_agent --write --auto-approve-commands "修复测试失败并运行 pytest"
```

测试：

```powershell
python -m pytest
```

如果没有激活虚拟环境，也可以直接用：

```powershell
py -3.12 -m pytest
```

## 当前架构

```text
coding_agent/
  cli.py          CLI 入口，解析参数和环境变量
  config.py       配置加载和校验
  agent.py        Responses API 代理循环
  model_client.py OpenAI Responses API 客户端抽象，便于 mock 和替换实现
  prompts.py      系统提示和任务提示
  context.py      workspace 文件扫描和上下文采样
  tools.py        模型工具：读取、补丁、搜索、Git 状态/差异和命令验证
  search.py       workspace 文本搜索工具
  patch.py        unified diff 解析、校验和应用
  path_safety.py  workspace 路径安全校验
  types.py        共享类型
tests/
  test_agent.py
  test_search.py
  test_tools.py
  test_integration.py
  fixtures/failing_project/
  test_path_safety.py
  test_patch.py
docs/
  implementation-plan.md
  m1-learning-and-interview.md
  m2-implementation-guide.md
```

## 安全模型

第一版只实现基础边界，后续需要继续强化。

- 文件路径必须解析在 workspace 内。
- 默认 `read-only`；`write_file` 已停用，直接写入调用会被拒绝。
- `apply_patch` 会先解析 unified diff、校验路径和 hunk 上下文，并在写入前展示完整 diff。
- `search_text` 会限制搜索路径在 workspace 内，并跳过常见构建目录和二进制文件。
- `run_command` 会拦截明显修改文件的命令，除非开启 `--write`。
- 命令执行默认需要人工确认。
- 不自动执行高风险 Git 操作。

后续计划加入更严格的命令 allowlist、沙箱进程、审批日志和会话回放。

## OpenAI API 路线

本项目使用 OpenAI Responses API 作为代理循环基础。Responses API 适合把模型输出、工具调用、推理状态和多轮执行组织在同一个接口里；对于更复杂的多代理编排、 tracing、handoff 和长期会话，后续可以评估接入 OpenAI Agents SDK。

模型调用集中在 `coding_agent/model_client.py`。`agent.py` 只依赖 `ModelClient` 协议，因此单元测试可以注入 fake client，不需要真实 API key。

## 开发状态

这是早期版本，不是完整的 Codex 替代品。它已经具备可扩展骨架，但仍缺少：

- 更强的 shell 沙箱
- 流式输出
- 会话持久化
- 更完整的测试矩阵
- IDE/编辑器集成
