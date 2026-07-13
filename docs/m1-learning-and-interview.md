# M1 学习指南与面试问答

本文面向已经完成 M1“可靠的本地编辑器”的开发者，目标是帮助你理解自己实现了什么、背后的工程知识是什么，以及如何在面试中清楚地讲解这个项目。建议结合 `coding_agent/` 源码和 `tests/test_integration.py` 阅读。

## 一、应该掌握的知识地图

| 主题 | 对应代码 | 掌握目标 |
| --- | --- | --- |
| CLI 与配置 | `cli.py`、`config.py` | 理解参数、环境变量、默认值和权限模式的合并 |
| Agent 循环 | `agent.py` | 理解模型响应、工具调用、结果回传和终止条件 |
| 模型抽象 | `model_client.py` | 理解 Protocol、依赖倒置和 mock 测试 |
| 工具调用 | `tools.py` | 理解 JSON Schema、参数解析、权限检查和统一结果 |
| Diff-first 编辑 | `patch.py` | 理解 unified diff、hunk 校验、规划与应用分离 |
| 路径安全 | `path_safety.py` | 理解路径穿越和 workspace 边界 |
| Git 验证 | `git_status`、`git_diff` | 理解代码修改后的可审查性 |
| 自动化测试 | `tests/` | 区分单元测试、集成测试和真实模型端到端测试 |

## 二、核心知识问答

### Q1：M1 阶段解决的核心问题是什么？

**答：** M1 把项目从“能够调用模型和工具的 CLI 骨架”推进为“能够安全修改本地代码的 Agent”。核心闭环是：读取文件、生成补丁、展示 diff、获得审批、应用修改、查看 Git 差异并运行测试。重点不是让模型拥有任意写权限，而是让每次代码编辑都可检查、可拒绝、可验证。

### Q2：Agent 循环是怎样工作的？

**答：** `run_agent()` 首先采集 workspace 上下文，然后调用 Responses API。若响应包含 function call，就执行对应工具，把结果包装为 `function_call_output`，再通过 `previous_response_id` 继续请求。没有工具调用时返回最终文本；超过 `max_turns` 时抛出错误，避免无限循环。

基本流程如下：

```text
用户任务 → workspace snapshot → 模型响应
                              ↓
                         function call
                              ↓
                       execute_tool()
                              ↓
                  function_call_output 回传
                              ↓
                       最终回答或继续
```

### Q3：为什么要定义 `ModelClient` Protocol？

**答：** 它让 Agent 依赖抽象，而不是直接依赖 OpenAI SDK。生产环境使用 `OpenAIResponsesClient`，测试环境注入 fake client。这样可以稳定构造工具调用序列，不消耗 API 配额，也不会受网络、模型随机性和服务状态影响。这是依赖倒置和可测试性设计的实际应用。

### Q4：Function Calling 中的工具定义有什么作用？

**答：** `TOOL_DEFINITIONS` 使用 JSON Schema 描述工具名、用途、参数类型、必填字段和是否允许额外参数。模型据此生成结构化调用，程序再通过 `execute_tool()` 解析参数并分发。Schema 只能约束调用格式，真正的安全边界仍必须由本地代码实施，不能信任模型会主动遵守提示词。

### Q5：为什么停用 `write_file`，强制使用 `apply_patch`？

**答：** `write_file` 会完整覆盖文件，难以判断模型改了哪些行，也容易误删原内容。`apply_patch` 使用 unified diff 表达增删改，应用前会展示完整差异，并校验目标路径、文件状态、hunk 行数和上下文。即使旧响应仍调用 `write_file`，工具层也会拒绝，因此限制不只存在于提示词中。

### Q6：Unified diff 包含哪些关键结构？

**答：** `---` 表示旧文件，`+++` 表示新文件，`@@ -old_start,old_len +new_start,new_len @@` 描述 hunk 范围。hunk 中空格开头是上下文，`-` 是删除行，`+` 是新增行。新增文件使用 `/dev/null` 作为旧路径，删除文件则把新路径设为 `/dev/null`。

```diff
--- a/calculator.py
+++ b/calculator.py
@@ -1,2 +1,2 @@
 def add(left: int, right: int) -> int:
-    return left - right
+    return left + right
```

### Q7：为什么 Patch 要分为 `plan_patch()` 和 `apply_patch_plan()`？

**答：** `plan_patch()` 先解析并验证所有文件变更，计算修改前后的内容；确认无误后，`apply_patch_plan()` 才真正写盘。这样能在写入前发现路径逃逸、文件不存在或上下文不匹配。当前写入阶段还不是事务性的：如果多文件写入过程中发生系统错误，仍可能出现部分完成，后续可以通过临时文件、备份或原子替换增强。

### Q8：如何防止路径穿越？

**答：** `resolve_inside_workspace()` 把 workspace 和请求路径解析为绝对规范路径，然后确认结果等于 workspace 或位于其子目录中。像 `../secret.txt` 这样的路径会被拒绝。安全检查必须在每个文件工具内部执行，不能只检查用户最初输入的 workspace。

### Q9：当前权限模型是什么？

**答：** 默认是 `read-only`，允许读取、搜索和查看 Git 状态；`--write` 切换为 `workspace-write`，才允许应用补丁。补丁默认需要人工确认，`--auto-approve-edits` 只适合测试或受控环境。命令默认也需要确认，`--auto-approve-commands` 会跳过确认，因此风险高于普通只读运行。

### Q10：`run_command(shell=True)` 有什么风险？

**答：** Shell 字符串可能包含重定向、管道、命令拼接、删除操作或环境变量泄露。当前实现会识别部分明显修改命令，并要求权限和人工审批，但正则判断无法覆盖所有变体。更安全的方向是提供结构化工具，例如 `run_pytest(args)`、`run_git(args)`，使用参数数组执行并配合 allowlist、超时、资源限制和沙箱。

### Q11：为什么编辑后还要调用 `git_diff`？

**答：** 补丁预览解决“写入前审查”，`git_diff` 解决“写入后确认”。它可以验证实际工作区内容是否与预期一致，也帮助最终回答准确列出修改。需要注意，普通 `git diff` 对未跟踪文件和某些 staged 变更展示有限，后续可以组合 `git status`、cached diff 和未跟踪文件预览。

### Q12：如何测试一个带模型的 Agent？

**答：** 测试重点不是预测真实模型会说什么，而是控制模型响应并验证程序行为。Fake client 按顺序返回 `read_file`、`apply_patch`、`git_diff`、`run_command` 和最终回答。测试再检查工具结果、磁盘内容和 pytest 输出，从而得到确定、快速、无需 API key 的测试。

### Q13：单元测试、集成测试和端到端测试有什么区别？

**答：** 单元测试单独验证 patch 解析、路径安全或文本搜索；集成测试把 Agent、fake model、工具、Git 和 fixture 项目连接起来；端到端测试会调用真实模型完成任务。真实模型测试成本高、存在随机性，通常手动运行，不应作为普通 CI 的唯一质量门槛。

### Q14：M1 集成测试证明了什么？

**答：** `tests/test_integration.py` 先确认 fixture 测试失败，然后让 Agent 读取错误代码、应用补丁、检查 Git diff、运行 pytest，并确认结果变为通过。这证明主要组件能够组成完整工作流，而不仅是每个函数单独可用。

### Q15：M1 之后仍有哪些技术债？

**答：** Patch 暂不支持 rename、mode change 和 binary diff；多文件写入不是事务；缺少持久化审批日志；shell 策略仍较粗；symlink、junction 和敏感文件规则还需加强；上下文采样也没有真正解析 `.gitignore` 或进行语义检索。这些分别属于后续安全、会话和项目理解阶段。

## 三、动手复习问答

### Q1：如何验证全部测试？

**答：** 激活 Python 3.12 虚拟环境后运行：

```powershell
.\.venv\Scripts\python.exe -m pytest -q --basetemp=.coding-agent\pytest-final
```

当前 M1 验收基线为 17 个测试通过。

### Q2：如何手工验证只读模式？

**答：** 不传 `--write` 启动任务，让模型尝试修改文件。预期 `apply_patch` 返回只读模式错误，文件不发生变化。

### Q3：如何手工验证补丁审批？

**答：** 使用 `--write`，但不要使用 `--auto-approve-edits`。工具应先打印变更摘要和完整 unified diff，再询问 `Apply patch? [y/N]`；输入 `n` 后文件必须保持不变。

### Q4：如何验证路径安全？

**答：** 运行：

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_path_safety.py tests\test_patch.py -q
```

重点观察 `../outside.txt` 和 hunk 上下文不匹配是否被拒绝。

## 四、面试高频问题与参考回答

### 面试题 1：请介绍一下这个项目。

**参考回答：** 这是一个 Python 3.12 实现的本地 AI 编程 CLI。我负责的 M1 阶段重点是可靠编辑：模型通过 Responses API 进行多轮工具调用，读取 workspace 后只能提交 unified diff，工具会校验路径和 hunk 上下文、展示完整 diff、等待审批，再应用修改并通过 Git diff 和 pytest 验证。为了保证可测试性，我抽象了 ModelClient，并使用 fake model 完成无需真实 API 的集成测试。

### 面试题 2：你的 Agent 如何避免无限调用工具？

**参考回答：** Agent 使用 `max_turns` 限制工具循环次数。每轮提取 function call、执行并回传结果；当响应不再包含工具调用时结束。如果达到限制仍未结束，就抛出明确错误。生产化后还可以增加总时间、Token、命令次数和失败重试预算。

### 面试题 3：提示词写了“不要修改文件”是否足够安全？

**参考回答：** 不够。提示词只是行为引导，模型可能出错或被恶意内容诱导。真正的权限控制必须在工具层完成。本项目在只读模式拒绝 `apply_patch`，对路径做 workspace 校验，并完全停用 `write_file`，所以即使模型生成违规调用，本地执行层也不会直接写入。

### 面试题 4：为什么不用模型直接返回完整文件？

**参考回答：** 完整文件覆盖的审查成本高，也容易因上下文截断删除未关注的内容。Diff 能表达最小变更，便于审批、上下文校验、Git 对比和回滚，更符合代码评审工作流。

### 面试题 5：Patch 上下文校验解决了什么问题？

**参考回答：** 它防止补丁应用到已经变化或与模型认知不一致的文件。如果 hunk 中的上下文和删除行与当前文件不匹配，系统拒绝写入，而不是在错误位置盲目修改。这属于乐观并发控制的一种简化形式。

### 面试题 6：你的 Patch 实现有哪些边界情况？

**参考回答：** 已覆盖新增、修改、删除、hunk 行数、上下文不匹配、CRLF/LF 和路径逃逸。当前不支持 rename、mode change、binary diff，也没有事务性多文件提交。这些是我会继续补充的能力。

### 面试题 7：为什么使用 Protocol，而不是直接 mock OpenAI SDK？

**参考回答：** Protocol 定义了项目真正需要的最小接口，减少业务代码对第三方 SDK 对象结构的耦合。测试 fake 只需实现初始响应和工具响应两个方法，既容易理解，也能在将来替换 provider 或 SDK 时保持 Agent 核心稳定。

### 面试题 8：你如何保证测试不调用真实模型？

**参考回答：** `run_agent()` 接受可选的 `model_client`。测试注入按状态机返回响应的 fake client，所有工具调用参数和结果都可断言，因此不需要 `OPENAI_API_KEY`，也没有网络成本和随机性。

### 面试题 9：集成测试为什么要先运行一次失败测试？

**参考回答：** 如果只检查修改后 pytest 通过，fixture 可能一开始就是通过的，测试无法证明 Agent 真正修复了问题。先断言 `1 failed`，再执行 Agent 并断言 `1 passed`，才能证明状态发生了预期变化。

### 面试题 10：`shell=True` 应该如何改进？

**参考回答：** 我会优先把常用操作做成结构化工具，使用参数数组而不是拼接字符串；为 Git、pytest、formatter 建立 allowlist；阻止网络和敏感文件访问；记录命令、退出码和输出；最后在 Docker 或受限子进程中运行。正则检测只能作为辅助，不能作为完整沙箱。

### 面试题 11：如果补丁涉及多个文件，怎样避免只写入一半？

**参考回答：** 当前实现会先规划和验证全部文件，减少验证阶段的部分写入，但应用阶段仍非事务。可以先把新内容写入同文件系统的临时文件，保存原文件快照，然后使用原子替换；任何一步失败时按快照回滚。还应对并发修改增加内容哈希或版本检查。

### 面试题 12：如果让你继续开发 M2，你会先做什么？

**参考回答：** 我会先解析 `.gitignore` 和 `AGENTS.md`，增加多文件批量读取和基于文件名、路径、语言及搜索命中的相关性排序；随后接入 `rg`，减少全量上下文。验收目标是让模型在中型项目中能够自主搜索并定位文件，而不是把大量源码一次性塞入提示词。

## 五、项目表达模板

### 30 秒版本

我实现了一个本地 AI 编程 CLI 的可靠编辑阶段。它通过 Responses API 驱动工具循环，默认只读，写入时强制使用 unified diff，执行前校验路径和上下文并展示完整 diff，执行后用 Git diff 和 pytest 验证。我还通过 Protocol 抽象模型客户端，用 fake model 完成了从失败测试到修复通过的确定性集成测试。

### 2 分钟版本应包含

1. 项目要解决的问题：让 AI 安全地读取、修改和验证本地代码。
2. 核心架构：CLI、配置、上下文、Agent 循环、模型抽象、工具层。
3. M1 的关键决策：默认只读、停用直接写入、强制 diff-first。
4. 安全实现：workspace 路径校验、hunk 上下文校验、审批和命令限制。
5. 测试策略：单元测试加 fake model 集成测试。
6. 局限和下一步：shell 沙箱、事务性 patch、会话审计和 M2 项目理解。

## 六、自检清单

当你能够不看代码回答以下问题时，可以认为已经掌握 M1：

- [ ] 能画出 Responses API 工具调用循环。
- [ ] 能解释 JSON Schema 与本地权限检查的区别。
- [ ] 能手写并解释一个 unified diff。
- [ ] 能说明为什么 `write_file` 被停用。
- [ ] 能解释路径穿越和当前防护方式。
- [ ] 能说明 `shell=True` 的风险和替代方案。
- [ ] 能区分单元测试、集成测试和真实模型 E2E 测试。
- [ ] 能讲清 fake model client 如何驱动确定性测试。
- [ ] 能指出当前 Patch 的非事务性问题。
- [ ] 能用 30 秒和 2 分钟两个版本介绍项目。
