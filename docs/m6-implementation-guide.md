# M6 阶段详细实现指南：产品化体验

## 1. 阶段目标

M5 已经完成结构化命令、安全策略、受控 host runner、可选 Docker sandbox、full-auto 门禁和可恢复审计链路。当前主要问题不再是“能不能安全完成任务”，而是“用户能否持续看懂、控制并复用这条执行链路”：

- `agent.py`、`cli.py` 和 `approvals.py` 直接调用 `print()`/`input()`，输出规则分散；
- 模型响应完成后才整体显示，长任务缺少及时反馈；
- 工具状态、验证、审批和恢复信息没有统一的终端呈现；
- agent 没有可持久化的结构化计划，resume/replay 也无法展示计划进度；
- run、review、explain 共用同一任务入口，权限边界和输出合同不清晰；
- 编辑器只能手工打开终端，尚无最小集成入口。

M6 的目标是建立一层薄的产品体验外壳，不重写 M1-M5 的 agent、安全和 session 核心：

```text
Agent / approvals / verification / SessionStore
                    ↓
             脱敏 UI event stream
              ↙               ↘
      line-oriented terminal     JSONL consumer
              ↓                       ↓
       人工交互与审批             VS Code prototype
```

完成后应支持：

1. 模型文本增量显示，工具、审批、验证和恢复状态及时可见；
2. TTY、重定向输出、`NO_COLOR`、Windows 和 POSIX 下行为稳定；
3. 计划由结构化工具更新，进入 SessionStore、resume 和 replay；
4. `run`、`review`、`explain` 使用明确且不可越权的工具集合；
5. 人类输出与机器可读 JSONL 共用同一事件合同；
6. VS Code 原型只调用现有 CLI，不新增 daemon、RPC 或第二套安全策略。

M6 不实现全屏 TUI、Web UI、远程会话同步、多代理编排、后台 daemon、遥测平台、插件系统、VS Code Marketplace 发布或自定义模型网关。出现真实需求后再分别规划。

## 2. 学习目标与先修知识

建议先掌握：

- `sys.stdout`、`sys.stderr`、`isatty()`、管道、broken pipe 和退出码；
- ANSI 基础、`NO_COLOR` 约定和 Windows 终端差异；
- Python iterator/callback、增量 UTF-8 文本和流式响应的完成/中断语义；
- 当前 `ModelClient → normalize_model_response() → agent loop` 调用链；
- M4/M5 的 SessionStore、reducer、resume、replay、审批和安全事件；
- VS Code `ProcessExecution` 的 argv 执行方式，以及为什么不能拼接 shell 文本。

开始前固定 M5 基线：

```powershell
.\.venv\Scripts\python.exe -m pytest -q --basetemp=.pytest-tmp-m6-baseline
```

当前基线为 640 passed，真实 Docker smoke 与平台不支持的 symlink 案例允许按既有条件跳过。

## 3. 范围与成功指标

### 3.1 用户可见指标

- 新任务在模型请求前显示 session、workspace、mode、permission 和 sandbox 摘要；
- 模型输出可增量显示，最终拼接文本与持久化的 normalized response 完全一致；
- 工具至少显示 started、passed/failed、耗时和安全后端，不泄露秘密或完整环境；
- 审批提示出现前先结束当前增量行并 flush，用户输入不会与流式文本混在一起；
- Ctrl+C 返回 130，终止活动流/进程树并持久化 `session.interrupted`；
- 非 TTY 输出不使用光标控制、spinner 或 ANSI；`NO_COLOR`/`--no-color` 禁用颜色；
- JSONL 模式 stdout 每行都是一个完整 JSON object，诊断信息只写 stderr；
- review/explain 默认不写 workspace、不请求命令审批，也不提供通用进程工具；仅允许已有的固定只读 Git 查询；
- resume 恢复任务 mode 和 plan，不重复已完成工具或已提交 review findings。

### 3.2 回归指标

- M1-M5 全量测试继续通过；
- streaming on/off 不改变最终 answer、tool calls、approval facts 和 session 事件语义；
- terminal renderer 测试不依赖真实 TTY、网络、Docker 或 OpenAI API key；
- 默认测试不使用固定 sleep 等待 UI；所有事件顺序通过 fake client/sink 确定性验证；
- 真实模型、终端和 VS Code smoke 保持 opt-in。

## 4. 设计原则

### 4.1 SessionEvent 仍是事实，UiEvent 只是投影

SessionStore 决定 resume/replay 的事实顺序，UI 事件只服务当前进程的展示和机器消费。不要把 spinner frame、文本 delta 或终端宽度写入 session；不要让 renderer 反向驱动 reducer。

### 4.2 streaming 只改变传输，不改变 agent 语义

增量文本先发送到 UI，同时在内存中累积；只有 SDK 明确完成响应后，才构造现有 raw response 并进入 `normalize_model_response()`。不得在收到部分 delta 后自动重发非流式请求，否则可能重复计费、工具调用或副作用。

### 4.3 一份事件合同服务所有前端

terminal、JSONL 和 VS Code 使用相同的 `UiEvent`。终端可以选择颜色和压缩展示，JSONL 保留稳定字段；两者不得各自在 agent 核心中维护状态机。

### 4.4 stdout 是数据通道，stderr 是人类诊断通道

human 模式可以把正常交互写 stdout；JSONL 模式的 stdout 只能写 JSONL。traceback、配置错误和启动失败写 stderr，避免破坏管道消费者。

### 4.5 产品模式不能扩大安全权限

`review` 和 `explain` 是比 `run` 更窄的工具 profile。mode 在 session 创建时持久化，resume 不接受覆盖。UI flag、颜色、streaming 或编辑器入口都不能绕过 M5 的审批、命令策略和 sandbox 门禁。

### 4.6 先做 line-oriented UI，再评估全屏 TUI

标准库文本输出已经覆盖交互终端、CI、日志、SSH 和编辑器终端。M6 不引入 Rich/Textual/curses；只有当实际可用性测试证明行式 UI 不足时，再单独评估依赖和全屏状态管理。

### 4.7 取消、输出错误和终端关闭都是正常路径

Ctrl+C、关闭 pipe、renderer 写入失败和流中断不能留下未关闭进程或损坏 session。安全清理优先于漂亮的终端结尾。

## 5. 建议文件与最小领域模型

优先保持扁平模块，不新建产品框架：

```text
src/coding_agent/
  ui.py             UiEvent、UiEmitter、TerminalRenderer、JsonlRenderer
  plans.py          PlanItem、PlanState 和 update_plan 参数校验
  task_modes.py     run/review/explain 的提示词和工具 profile
editors/vscode/
  package.json      三个命令和配置声明
  extension.js      使用 ProcessExecution 调用 coding-agent argv
tests/
  test_ui.py
  test_m6_step8.py
  test_task_modes.py
  test_m6_integration.py
  test_m6_acceptance.py
```

如果 `ui.py` 在实现后确实同时承担事件模型、终端状态和 JSON 编码且难以测试，再拆成 `ui_events.py` 与 `terminal.py`；不要预先建只有一个实现的 renderer factory 或 plugin registry。

最小事件模型：

```python
@dataclass(frozen=True)
class UiEvent:
    schema_version: int
    seq: int
    type: str
    payload: Mapping[str, JsonValue]

UiEventHandler = Callable[[UiEvent], None]
```

首版事件词表固定为：

```text
run.started
model.started
model.output.delta
model.finished
tool.started
tool.finished
approval.requested
approval.decided
verification.finished
plan.updated
run.finished
run.interrupted
run.failed
```

`UiEmitter` 只负责递增 `seq`、冻结/校验 JSON payload 和调用一个 handler。不要加入 event bus、订阅列表、异步队列或跨进程 transport。

## 6. 详细实现步骤

### 6.1 第一步：冻结 M6 验收合同

先新增 `tests/test_m6_acceptance.py`，固定产品行为，暂不改生产代码：

1. M1-M5 的 640 项基线属于 M6 验收；
2. UI event 使用 `schema_version = 1`、连续 seq 和固定 type 词表；
3. JSONL 每行可独立 `json.loads()`，不包含 ANSI 或非 JSON 前缀；
4. non-TTY、`NO_COLOR` 和 `--no-color` 禁止 ANSI/光标控制；
5. 流式 delta 拼接结果等于最终 normalized text；
6. streaming 不产生 session delta 事件，不改变已有 durable event types；
7. renderer payload 不含测试注入的 API key/token/secret/password；
8. review/explain 的写工具、`run_command`、`run_verification` 和 input 调用数为 0；除固定只读 Git 查询外不启动 subprocess；
9. `update_plan` 有固定数量/长度预算且拒绝未知字段；
10. Ctrl+C 返回 130，并记录 interrupted 事实；
11. VS Code command 使用 executable + argv，不使用 shell 字符串；
12. 默认测试不要求真实 TTY、模型、Docker、Node 或 VS Code。

测试合同应引用现有安全常量和 tool policy，避免复制第二套权限真相。

建议命令：

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_m6_acceptance.py -q --basetemp=.pytest-tmp-m6-contract
```

**实现状态（已完成）**：新增 `tests/test_m6_acceptance.py`，以 12 个合同测试固定 M1-M5 的 640 passed 参考基线、UI schema/version/type/连续 seq、JSONL 通道、non-TTY/颜色边界、streaming 拼接与 durable session 分离、秘密值排除、review/explain 工具权限、plan schema/预算、Ctrl+C 退出码、VS Code argv 执行和可选 smoke 依赖。合同只引用现有 `SESSION_EVENT_TYPES` 与 `TOOL_POLICIES` 事实源，不导入或创建后续 UI、plan、task mode 和 editor 生产模块。聚焦测试 12 passed；当前受限 Windows 环境的全量回归为 636 passed、18 skipped，无失败。

### 6.2 第二步：定义脱敏 UI event contract

新增 `ui.py`：

- `UiEvent` 构造时校验版本、正整数 seq、已知 type 和 JSON-compatible payload；
- `UiEmitter.emit(type, payload)` 生成连续事件并同步调用 handler；
- 默认 handler 为空操作，库调用方无需终端也能运行；
- payload 复用 session privacy 的敏感值清理，但不 artifact 化 UI delta；
- tool event 只包含 call ID、tool name、状态、耗时、backend 和截断摘要；
- model delta 只包含用户可见 output text，不发送隐藏 reasoning、raw SDK object 或完整 prompt；
- handler 异常转换为明确的 UI 输出错误，不得默默吞掉或破坏 session writer。

先只写模型和单元测试，不接 agent。确保 mapping 在传给 handler 后不可被调用方修改。

**实现状态（已完成）**：新增 `src/coding_agent/ui.py`，固定 `UI_SCHEMA_VERSION = 1`、13 个 event type、严格 `to_dict()`/`from_dict()`、事件专用 payload 边界和同步 `UiEmitter`。`UiEvent` 复用 `SessionPrivacyPolicy` 清除环境秘密、认证/进程上下文字段并在无 artifact writer 时安全省略大值；payload 通过从 session 模型公开的 `freeze_json_object()` 深度冻结。emitter 默认无 handler，按 1 开始分配连续 seq；handler 异常包装为带 event type/seq 的 `UiHandlerError` 并保留原始 cause。`tests/test_ui.py` 覆盖 schema、非法版本/type/JSON、严格反序列化、深度不可变、连续同步投递、秘密与大值、model delta/tool payload 和 handler failure，共 22 passed；当前受限 Windows 环境全量回归 658 passed、18 skipped。尚未接 agent 或 renderer。

### 6.3 第三步：集中 terminal 与 JSONL renderer

在同一模块实现两个直接 renderer：

#### TerminalRenderer

- 构造参数为 stdout、stderr、stdin、`is_tty`、color enabled；测试使用 `io.StringIO`；
- TTY 模式允许最少 ANSI 颜色，但首版不重写旧行、不画 spinner；
- non-TTY 使用稳定的一事件一行文本；
- 支持 `NO_COLOR` 和 CLI `--no-color`，CLI 优先；
- 长文本继续复用现有 console truncation，不复制新的预算算法；
- 遇到 broken pipe 停止渲染并让 CLI 返回 Unix 习惯的非错误管道结果，不打印 traceback。

#### JsonlRenderer

- stdout 每个 event 输出一行 compact UTF-8 JSON 并 flush；
- key 顺序稳定，使用 `ensure_ascii=False`；
- 不输出 banner、颜色、审批装饰或 traceback；
- UI 诊断与无法序列化错误写 stderr；
- schema 不直接暴露 SessionEvent payload 或 artifact 正文。

此步骤不修改 agent 行为，只验证 renderer 对同一事件序列产生两个投影。

**实现状态（已完成）**：在 `src/coding_agent/ui.py` 中新增可直接作为 `UiEmitter` handler 的 `TerminalRenderer` 与 `JsonlRenderer`，未引入 renderer factory 或第三方终端依赖。terminal 投影支持 TTY 增量文本、最少 SGR 颜色、稳定 non-TTY 单行文本、`NO_COLOR`/显式禁色、交互前行边界 flush、控制字符安全化和 broken pipe 静默关闭；长人类输出复用从 agent 提取的 2000 字符 console budget，现有 agent 输出行为不变。JSONL 投影使用 `ensure_ascii=False`、稳定排序和 compact separators，每个事件独占 stdout 一行并 flush，诊断/序列化错误仅写 stderr，broken pipe 后停止输出。第三步完成时 `tests/test_ui.py` 为 35 passed，覆盖 TTY/non-TTY、颜色、截断、Unicode JSONL、通道隔离、序列化错误、流参数校验与 broken pipe；当时受限 Windows 环境全量回归 671 passed、18 skipped。本步尚未把 renderer 接入 agent/CLI。

### 6.4 第四步：把散落的 print/input 接到 UiEmitter

按真实调用链逐个迁移：

1. `cli.py` 创建 emitter/renderer，并发出 `run.started`；
2. `agent.py` 的 session、model、tool、verification 和 final 输出改为 emit；
3. `approvals.py` 在展示请求和记录决定时 emit；
4. replay/list/approvals 的现有 `--json` 保持原合同，不强制改成 live JSONL；
5. 测试和库调用可传空 handler，不产生 console 副作用。

审批输入仍由现有 approval handler 负责，不创建通用 input service。TerminalRenderer 只提供“结束增量行、flush、显示请求”的小方法；identity binding 和 auto-approval 仍由 M4/M5 代码决定。

迁移后生产 agent/approval 路径不应再直接 `print()`，CLI 的纯查询格式化函数可以暂时保留，直到它们需要共用 live renderer。

**实现状态（已完成）**：CLI 的 new/resume 路径现在在创建 SessionStore/model client 前选择一个 `TerminalRenderer` 和 `UiEmitter`，发出唯一的 `run.started` 并把同一 emitter 传入 agent；list/replay/approvals 及其既有 `--json` 合同保持不变。`run_agent*`/`resume_agent*` 接受可选 emitter，默认使用无 handler emitter，因此库调用没有普通 console 输出；模型请求/完成、工具开始/完成、验证结果和持久化后的 `run.finished` 都从真实状态边界 emit。审批包装器在调用 handler 前发出脱敏 `approval.requested`，在 `approval.decided` durable event 落盘后发出同名 UI event；交互输入仍由原 handler 负责，无法完整展示的超大审批请求会 fail closed。生产 agent 路径已移除直接 `print()`，直接调用 `execute_tool()` 的旧审批展示通过注入的 request writer 保持兼容。terminal renderer 对模型正文、工具摘要和审批详情做专用展示，避免 final answer 重复；新增/更新测试固定连续事件顺序、无 handler 静默、审批事实顺序、non-TTY 完整审批和 session-before-model 边界。当前受限 Windows 环境全量回归 673 passed、18 skipped。本步未增加 live JSONL CLI 选项或模型 streaming。

### 6.5 第五步：接入模型 output streaming

保持 `ModelClient` 的两个现有请求方法可被 fake clients 实现。只在 `OpenAIResponsesClient` 内增加可选 UI handler，并使用当前受支持的 Responses streaming API：

1. 收到 output text delta 时 emit `model.output.delta` 并累积文本；
2. 收到 function call、reasoning summary和完成数据时累积完整 raw response；
3. 流正常完成后仍调用现有 `normalize_model_response()`；
4. 持久化的 `model.responded` 只记录完整 normalized response；
5. 部分流后断开时记录失败/中断，不自动重新发送请求；
6. `--no-stream` 使用现有非流式请求，便于日志、兼容性和故障排查；
7. fake client 默认保持非流式，无需所有旧测试模拟 SDK event。

不得显示 hidden chain-of-thought。reasoning summary 只有在 API 明确返回可展示 summary 时，才作为完成事件的一部分显示。

流式测试使用本地 fake event iterator，至少覆盖：Unicode 被拆分、空 delta、工具调用、正常结束、API error、Ctrl+C 和完成事件缺失。

**实现状态（已完成）**：`OpenAIResponsesClient` 默认通过当前 Responses `stream=True` 事件迭代器接收响应，仅将非空 `response.output_text.delta` 投影为脱敏的 `model.output.delta`，同时在内存中拼接文本；`response.completed` 提供的完整响应继续进入既有 `normalize_model_response()`，并校验最终 normalized text 与 delta 拼接一致。function call 和 API 明确返回的 reasoning summary 只从完整完成响应归一化，reasoning delta/hidden chain-of-thought 不进入 UI。`model.responded` 仍只持久化完整 normalized response，UI delta 不增加 durable event type；API error、failed/incomplete、迭代异常、Ctrl+C 和缺少完成事件都会关闭流、按现有 session 语义记录 failed/interrupted，且不自动重发。new/resume 默认启用 streaming，`--no-stream` 透传到内置 OpenAI client 并保留原非流式请求；显式 fake client 继续实现原两个完整响应方法。TerminalRenderer 在收到 delta 后不重复打印 completed/final 正文。本地 fake streaming 测试 8 passed，相关聚焦回归 100 passed；当前受限 Windows 环境全量回归 681 passed、18 skipped，`compileall` 与 `git diff --check` 通过。本步未增加 live JSONL CLI 输出选项。

### 6.6 第六步：完善工具、审批、验证与恢复状态

在 agent 已有边界发事件，不从 console 文本反解析：

- 工具执行前 `tool.started`；结束后 `tool.finished`，包括 ok/status/duration/backend/sandboxed；
- 审批展示前 `approval.requested`，事实写入后 `approval.decided`；
- 每次验证完成发 `verification.finished`，包含 command ID、kind、status、exit code 和截断标记；
- resume 对账过程显示 recovered/retry/reapproval 摘要，但事实仍来自已有 recovery events；
- sandbox capability、image digest 和 cleanup failure 只显示脱敏摘要；
- 并行工具尚未实现，因此首版 renderer 不需要并发 progress tree。

工具原始 stdout/stderr 只在现有预算内按需展示。默认成功命令显示摘要，失败命令显示压缩后的诊断，避免正常任务刷屏。

**实现状态（已完成）**：agent 在 durable `tool.started` 写入后、实际工具执行前发出 UI `tool.started`，并在 durable `tool.finished` 落盘后发出 UI `tool.finished`；UI 使用 `status` 投影工具 `ok`，同时携带 duration、backend、sandboxed、summary 与截断标记。审批请求先展示，审批决定 durable fact 成功写入后再投影；验证结果同样在 `verification.recorded` 后发出包含 command ID、kind、status、exit code、attempt、duration 和截断标记的 `verification.finished`。resume 会基于既有 `tool.recovered`/完成事实投影 safe retry、reapproval、recovered completion 摘要，不读取 renderer 文本参与恢复决策。sandbox capability、image digest、container 标识、capability/cleanup 原因只以脱敏状态摘要显示，工具输出统一使用现有 2000 字符 console 预算。异常和 Ctrl+C 先持久化 session terminal event，再各发一次脱敏 `run.failed`/`run.interrupted`，CLI 不重复终态投影。新增 13 项第六步聚焦测试，相关聚焦回归 90 passed；当前受限 Windows 环境全量回归 694 passed、18 skipped，`py_compile` 与 `git diff --check` 通过。本步未实现并发工具 progress tree。
### 6.7 第七步：增加稳定的 live JSONL 输出模式

CLI 新增：

```text
--output {human,jsonl}
--no-color
--no-stream
```

规则：

- 新任务和 resume 支持 `--output`；它是展示配置，不写入安全配置，也不改变 resume 语义；
- `--output jsonl` 隐含 no-color，但不隐含 no-stream；delta 也作为 JSONL event；
- JSONL 下的交互审批把结构化 request/decision event 写 stdout，把输入提示写 stderr，并继续从 stdin 读取；
- 既有 replay/list/approvals 的 `--json` 保持单个 JSON document，不与 live JSONL 混用；
- 对 query mode 传 `--output`、对 task mode 传旧 `--json` 时返回清楚的 usage error；
- JSONL `run.finished` 包含 session ID、final status 和 answer；调用方不需要读取人类文本；
- schema version 改动必须新增兼容测试，未知 event type 不得静默改变旧字段含义。

不要同时增加 SSE、WebSocket 或本地 socket；JSONL 已覆盖 pipe、日志和编辑器子进程。

**实现状态（已完成）**：CLI 新增 `--output {human,jsonl}` 与 `--no-color`，仅允许 new/resume 使用；默认仍为 human，`--output jsonl` 直接选择无 ANSI 的 `JsonlRenderer`，但不会改变 streaming，只有显式 `--no-stream` 才关闭 delta。new/resume 共用同一 `UiEmitter`，因此 `model.output.delta`、工具、审批、验证和 `run.finished` 均作为 compact JSON 独占 stdout 一行并即时 flush；终态包含 session ID、final status 和 answer。默认及 resume recovery 审批 handler 支持注入 input reader，JSONL CLI 将 prompt flush 到 stderr 后继续从 stdin 读取，结构化 request/decision 不被人类提示污染。query mode 显式传 `--output` 或 `--no-color`、task/resume 传旧 `--json` 均返回清楚错误；list/replay/approvals 的 `--json` 仍为单个 JSON document。schema 继续为 version 1，既有未知 type 严格拒绝测试保持通过，未新增网络传输协议。新增 `tests/test_m6_step7.py` 11 项测试，相关聚焦回归 82 passed；当前全量回归 721 passed、2 skipped，`py_compile` 与 `git diff --check` 通过。

### 6.8 第八步：实现可持久化计划与 `update_plan` 工具

只新增一个工具 `update_plan`，不分别创建 create/update/complete 三套 API。工具参数为完整计划：

```json
{
  "explanation": "optional short reason",
  "items": [
    {"step": "inspect parser", "status": "in_progress"},
    {"step": "add regression test", "status": "pending"}
  ]
}
```

固定约束：

- 1 至 20 项；step 去除首尾空白后 1 至 200 字符；
- status 仅 `pending`、`in_progress`、`completed`；
- 最多一个 `in_progress`；
- 拒绝未知字段、重复 step 和全 completed 后再次退回 pending；
- 工具 effect 为 session-only，不写 workspace、不需要审批；
- agent task mode 为 run/review/explain 时均可用，但简单任务不强制创建计划。

接入：

- 新增 `plan.updated` SessionEvent，reducer 保存最新不可变 PlanState；
- checkpoint、resume、replay 和 UI 读取 reducer 状态，不维护旁路 plan 文件；
- UiEmitter 发同名 `plan.updated` 投影，terminal 显示紧凑列表；
- resume 时未完成计划原样恢复，已完成项不能被自动重新执行；
- 旧 session 没有 plan 时使用空状态，保持兼容。

**实现状态（已完成）**：新增 `src/coding_agent/plans.py`，定义深度不可变的 `PlanItem`/`PlanState`、固定数量与长度预算、严格完整计划解析和终态转换校验；`update_plan` 是唯一新增工具，effect 为 `session_only`，在只读模式可用、不写 workspace 且不会触发审批。工具成功后按 `tool.started → plan.updated → tool.finished` 持久化事实，再投影同名 UI 事件；reducer 保存最新计划，checkpoint、resume 和 replay 均从 reducer state 读取，不创建旁路 plan 文件。terminal 使用 `[ ]`、`[>]`、`[x]` 显示计划，non-TTY 与 live JSONL 保持紧凑、结构化输出。旧 session/checkpoint 缺少 plan 时恢复为空计划，完成计划不能重新打开，中断的 session-only 调用可安全重试。新增 `tests/test_m6_step8.py` 21 项测试，相关聚焦回归 232 passed；当前全量回归 742 passed、2 skipped，`py_compile`、wheel 构建与 `git diff --check` 通过。

### 6.9 第九步：定义 run/review/explain task mode

CLI 新增 `--mode {run,review,explain}`，默认 `run`。为避免与现有 session/query `CliMode` 混淆，领域类型命名为 `TaskMode`。

mode 在 session.started config 中持久化；resume 不能覆盖。每种 mode 使用显式工具 profile：

| mode | workspace write | 通用进程工具 | 允许的核心工具 |
| --- | --- | --- | --- |
| run | 由现有 permission 决定 | 受 M5 policy/sandbox 控制 | 全部现有工具 + update_plan |
| review | 禁止 | 禁止；仅固定只读 Git | list/search/read/git_status/git_diff/update_plan/submit_review |
| explain | 禁止 | 禁止；仅固定只读 Git | list/search/read/git_status/git_diff/update_plan |

`--mode review|explain` 与 `--write`、自动批准或 full-auto 同时出现时直接 usage error，不静默忽略。工具 schema 必须在发给模型前按 profile 过滤；仅靠提示词说“不要写”不算权限边界。

每个 mode 使用独立的短 prompt fragment，公共安全和工具规则继续来自现有 system prompt，避免复制完整提示词。

**实现状态（已完成）**：新增 `task_modes.py`，以不可变 profile 统一定义 `run`、`review`、`explain` 的工具集合、workspace write 和通用进程能力。CLI 新增 `--mode`，受限 mode 会拒绝 `--write`、自动批准和 full-auto；mode 持久化到 session，resume 恢复旧值且旧 session 缺失字段时兼容为 `run`。模型初始请求与 continuation 都按同一 profile 过滤 schema，`execute_tool()` 在参数解析、审批和副作用前再次执行硬拒绝，并把拒绝作为无需审批的 preflight policy audit 持久化。三种 mode 使用独立短 prompt fragment；review/explain 只暴露 read/search、固定只读 Git 与 `update_plan`；第十步进一步只为 review profile 加入 `submit_review`。

### 6.10 第十步：实现结构化 review mode

新增一个 session-only 工具 `submit_review`，参数为：

```json
{
  "summary": "short overall assessment",
  "findings": [
    {
      "severity": "high",
      "path": "src/example.py",
      "line": 42,
      "title": "Unchecked destructive operation",
      "detail": "Why this matters and the smallest correction."
    }
  ]
}
```

约束：

- severity 仅 `critical|high|medium|low`；
- path 必须是 workspace-relative、非敏感的现有文本文件；
- line 是正整数且不超过当前文件行数；
- title/detail/summary 有明确字符预算；最多 50 findings；
- 相同 path/line/title 去重；没有问题时允许空 findings，但必须有 summary；
- 工具只为复检 path/line 读取目标文件，不返回正文、不写文件；
- 每次 review 只接受一次最终提交，resume 复用已完成结果。

AgentRunReport 增加可选 review result；human renderer 按 severity/path/line 排序显示，JSONL 在 `run.finished` 中返回结构化 findings。不要解析模型自由文本来重建 findings。

review 默认检查 workspace 当前 Git diff；用户任务可以限定目录或关注点。没有 Git 仓库时仍可 review 指定文件，但必须在摘要中说明输入范围。

**实现状态（已完成）**：新增不可变 `ReviewFinding` / `ReviewResult` 领域模型和 review-only、无需审批、session-only 的 `submit_review` 工具。工具使用严格 schema，并在提交时复检 severity、字符与总序列化预算、最多 50 条、去重、workspace-relative 路径、敏感路径、realpath/symlink、普通 UTF-8 文本文件和有效行号；复检过程不向模型返回文件正文。成功结果原子写入 `tool.finished.payload.review`，reducer、checkpoint、resume、replay 和旧 checkpoint 兼容逻辑都以该 durable fact 为准，崩溃发生在 `tool.finished` 前可安全重试、发生在其后直接复用。`AgentRunReport`、`session.completed` 与 `run.finished` 均携带结构化 review；human/TTY 按 severity、path、line、title 稳定排序，live JSONL 保持单行结构化输出。review mode 未成功调用一次 `submit_review` 时会失败，不从模型自由文本推断 findings。

### 6.11 第十一步：实现只读 explain mode

explain 不新增专用工具或第二套 agent loop：

- 使用 explain prompt fragment，要求先 search/read，再给出面向用户问题的说明；
- 引用使用 `path:line`，只引用实际读取过的 workspace 文件；
- 默认不展示 reasoning summary，只展示用户可见解释；
- 禁止 apply_patch、run_command、run_verification 和审批；
- 可解释当前文件、模块、Git diff 或 session replay 中已有事实；
- 找不到证据时明确说明，不用猜测填充；
- final answer 继续使用现有 text 字段，暂不创建 explanation AST。

测试确保模型即使请求写/进程工具，也会在工具 profile 边界前被拒绝，调用数为 0。

**实现状态（已完成）**：新增 `explanations.py` 中的不可变读取证据与 `path:line` 引用校验，但不新增 explain 专用工具、第二套 agent loop 或 explanation AST。成功的 `read_file` / `read_many_files` 会返回只包含实际展示行数、截断状态和规范化 workspace-relative path 的小型 `read_evidence`；agent 在可 artifact 化的工具正文之外，把该元数据单独写入 `tool.finished.payload.read_evidence`。最终 explain 文本从完整 session event log 合并 durable evidence，因此新运行和 resume 都只能引用已经成功持久化读取的文件，拒绝未读取路径、越界行号以及有可引用证据却没有引用的回答；没有读取证据时允许明确报告证据不足。normalized `model.responded` 继续保留 reasoning summary 以维持 session 事实，但 explain 的 `model.finished` UI 投影固定隐藏该字段，terminal 只显示用户可见解释。写入、命令和验证工具仍在共享 task-mode profile/dispatch 边界前失败且不会触发实现或审批。

### 6.12 第十二步：完善中断、resume、replay 和退出码体验

统一顶层结果：

| 情况 | exit code | durable 结果 |
| --- | ---: | --- |
| completed | 0 | session.completed |
| usage/config error | 2 | 未启动 session |
| policy/sandbox preflight failure | 1 | 未调用模型；若已有 session 则 failed |
| agent/runtime failure | 1 | session.failed |
| Ctrl+C | 130 | session.interrupted |
| stdout pipe closed | 0 | 不打印 traceback；已发生的 session facts 保留 |

resume/replay 增强：

- resume banner 显示 task mode、plan progress、permission、sandbox 和上次 phase；
- replay human 输出加入 plan 更新、review findings 和 UI 无关的最终状态；
- replay JSON schema 只在新增字段时向后兼容扩展，不记录 model delta；
- interrupted streaming response 没有完整 response ID 时，按现有 at-least-once 模型请求规则处理，不伪造完成；
- renderer 关闭不触发工具重试；安全清理和 session 终结先完成，再尝试最后一次输出。

**实现状态（已完成）**：CLI 已统一正常完成、usage/config、preflight/runtime、Ctrl+C 和 stdout pipe 关闭的退出码；agent 在 terminal UI 前持久化 `session.completed` / `session.failed` / `session.interrupted`，host runner 在中断传播前终止进程树并回收 reader。resume banner 展示持久化 task mode、permission、sandbox、上次 phase/status 和计划计数；resume preflight 漂移不调用模型，已有 session 会写入 durable failed 终态。未完成模型响应保持 pending request，不生成 synthetic response ID，resume 追加带 `retry_of_seq` 的 at-least-once 请求。replay schema 2 仅新增 `plan_updates` 和 `terminal`，human replay 展示计划历史、review findings、验证与 durable terminal 状态；UI delta 仍不进入 session timeline。renderer 或 stdout 关闭后 agent 继续完成清理和落盘，不重试工具，CLI 静默返回 0。第十二步回归集中在 `tests/test_m6_step12.py`。

### 6.13 第十三步：增加最小 VS Code prototype

在 CLI JSONL 和 task mode 稳定后再新增 `editors/vscode/`。首版只提供：

- `Coding Agent: Run Task`；
- `Coding Agent: Review Changes`；
- `Coding Agent: Explain Current File`；
- `codingAgent.executable` 配置，默认 `coding-agent`；
- 使用 `vscode.ProcessExecution(executable, args)` 启动，不拼 shell command；
- workspace 通过独立 argv 传递；current file 只传相对路径，不把选区正文放进命令行；
- 任务在 VS Code terminal 中运行，沿用 CLI 的审批、Ctrl+C、颜色和 session 输出；
- 多根 workspace 时要求用户选择 folder，不猜默认目录。

使用 plain JavaScript 和 VS Code 原生 API，不引入 bundler、framework、daemon 或 npm runtime dependency。M6 只做可从 Extension Development Host 加载的 prototype，不构建 Marketplace 发布、自动更新、账号体系或自定义 webview。

测试至少静态验证 manifest、command IDs、argv helper 和禁止 `shell: true`/字符串拼接。真实 VS Code smoke 手工或 opt-in 执行。

**实现状态（已完成）**：新增 `editors/vscode/` plain JavaScript prototype、无 npm runtime dependency 的 manifest、Extension Development Host 配置和纯 Node argv helper。三个命令统一使用可配置的 `codingAgent.executable`、`vscode.ProcessExecution` 参数数组及专用 task terminal；所选 folder 作为独立 `--workspace` argv，多根 workspace 必须显式选择。Run Task 启用 `--write`，Review Changes 选择 `--mode review`，Explain Current File 选择 `--mode explain`，并只把经过 traversal 校验和 `/` 规范化的 active file workspace-relative path 放入任务文本，不读取或传递 selection text。默认 Python 静态测试覆盖 manifest、command IDs、workspace trust/picker、ProcessExecution、禁止 shell API 和 Development Host 配置；Node 内建测试覆盖 argv 分离、shell 元字符、引号、跨分隔符规范化和路径逃逸。

### 6.14 第十四步：补齐产品与跨平台验收矩阵

建议测试文件：

```text
tests/test_m6_acceptance.py
tests/test_m6_step14.py
tests/test_ui.py
tests/test_model_streaming.py
tests/test_m6_step8.py
tests/test_task_modes.py
tests/test_review_mode.py
tests/test_explain_mode.py
tests/test_m6_integration.py
tests/test_cli_product.py
tests/test_session_replay.py
```

矩阵至少覆盖：

| 场景 | 预期 |
| --- | --- |
| TTY human output | 增量文本和状态可读，审批前 flush |
| redirected output | 无 ANSI、spinner、退格或光标移动 |
| `NO_COLOR`/`--no-color` | 所有 human 输出无颜色 |
| JSONL live output | 每行独立合法 JSON，stdout 无杂项 |
| Unicode delta 被任意切分 | 最终文本无乱码且等于 normalized response |
| streaming 中断 | 不自动重发；session interrupted/failed 可恢复 |
| tool output 超限 | 继续使用 M5 截断合同，不撑爆 UI |
| secret 出现在异常/工具输出 | UiEvent、terminal、JSONL 均不出现值 |
| approval 与 streaming 相邻 | 提示不与 delta 混行，决定身份绑定不变 |
| plan 更新非法 | reducer 和工具均拒绝，不污染旧 plan |
| resume 有未完成 plan | 恢复状态，不重复完成项 |
| review 请求写/命令 | schema 不提供或执行前拒绝，副作用为 0 |
| review finding 行号漂移 | 提交时重新校验并拒绝失效引用 |
| explain 请求写/命令 | 副作用为 0，返回只读限制 |
| Ctrl+C during model/tool | exit 130，模型/进程清理，session 可 replay |
| Windows/POSIX argv | CLI 与 VS Code 均不经 shell 拼接 |
| 旧 session | 无 mode/plan 字段时仍可 replay/resume |
| M1-M5 回归 | 全量通过 |

**实现状态（已完成）**：第十四步以 `tests/test_m6_step14.py` 固定 18 行验收矩阵，每一行都指向已收集的具体 pytest test function，而不是只检查文件存在。新增的产品级测试覆盖事件 emitter 到 terminal/JSONL renderer 的跨边界投影、审批与 partial delta 的换行和 flush、secret 在三类输出面中的脱敏、Windows/POSIX host runner 的原始 argv 与 `shell=False`、模型中断后的 durable `session.interrupted` replay、review 提交前的行号漂移复检、explain 的零副作用以及旧 session 缺省 plan 的 replay 兼容。`tests/test_m6_acceptance.py` 同时冻结产品测试模块清单；`pyproject.toml` 注册 `live_model`，默认验收命令排除 `docker`、`live_model` 和 `vscode`，不依赖外部运行时。验收结果为 M6 聚合测试 228 passed、核心矩阵 70 passed、产品集成 10 passed、默认矩阵 880 passed/1 skipped/1 deselected、全量 880 passed/2 skipped；`compileall`、Node argv helper、wheel 构建和 `git diff --check` 均通过。

第十四步定向矩阵命令：

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_m6_step14.py tests\test_m6_integration.py tests\test_cli_product.py tests\test_task_modes.py tests\test_review_mode.py tests\test_explain_mode.py -q
```

### 6.15 第十五步：更新文档、版本和最终验收

功能全部完成后：

- README 增加 human/JSONL、streaming、plan、review/explain 和 VS Code prototype 用法；
- `docs/implementation-plan.md` 仅在全部验收通过后把 M6 标为完成；
- `.env.example` 只增加必要且无秘密值的 UI 默认项；优先使用 CLI flag，不为每个颜色细节增加环境变量；
- 包版本建议从 `0.4.x` 升到 `0.5.0`；
- wheel 检查 `ui.py`、`plans.py`、`task_modes.py` 和 CLI metadata；
- VS Code prototype 独立检查 manifest，不强行塞进 Python wheel；
- 真实模型 streaming 和 VS Code smoke 为 opt-in，不阻塞默认离线测试。

建议最终命令：

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_m6_acceptance.py tests\test_ui.py tests\test_m6_step8.py -q --basetemp=.pytest-tmp-m6-core
.\.venv\Scripts\python.exe -m pytest tests\test_m6_integration.py tests\test_cli_product.py tests\test_review_mode.py tests\test_explain_mode.py -q --basetemp=.pytest-tmp-m6-integration
.\.venv\Scripts\python.exe -m pytest -m "not docker and not live_model and not vscode" -q --basetemp=.pytest-tmp-m6-default
.\.venv\Scripts\python.exe -m pytest -q --basetemp=.pytest-tmp-m6-full
.\.venv\Scripts\python.exe -m compileall -q src tests
.\.venv\Scripts\python.exe -m pip wheel . -w dist
git diff --check
```

只有配置好的受控环境再运行：

```powershell
.\.venv\Scripts\python.exe -m pytest -m live_model -q --basetemp=.pytest-tmp-m6-live
.\.venv\Scripts\python.exe -m pytest -m vscode -q --basetemp=.pytest-tmp-m6-vscode
```

**实现状态（已完成）**：README 已补齐 human/JSONL、streaming、plan、review/explain、resume/replay、退出码与 VS Code prototype 用法；总实施计划已把 M6 标为完成，包版本从 `0.4.0` 升至 `0.5.0`。新增 `tests/test_m6_step15.py` 固定版本一致性、文档完成状态、Python wheel 与 VS Code prototype 的独立边界，以及默认离线/opt-in smoke 合同。最终验收结果为第十五步合同 5 passed、核心矩阵 70 passed、产品集成 10 passed、默认离线矩阵 885 passed/1 skipped/1 deselected、全量 885 passed/2 skipped；`compileall`、Node argv helper 和 `git diff --check` 通过。离线构建生成 `dist/coding_agent-0.5.0-py3-none-any.whl`（181448 字节、49 个文件，SHA-256 `59635dcf455114508d987c0de5f93d720f48eef31666c7f6107fe843a1887889`）；wheel 的版本、Python 要求、依赖与 console entry 元数据正确，包含 `ui.py`、`plans.py`、`task_modes.py`、`reviews.py` 和 `explanations.py`，不包含 tests、`.env`、`.coding-agent/` 或 `editors/`，并通过离线隔离虚拟环境安装、包/SessionStore/CLI parser 导入和 `coding-agent --help` smoke。真实模型与 VS Code Development Host smoke 保持 opt-in，本机未执行。

## 7. 推荐实施顺序与依赖关系

```text
验收合同
   ↓
UiEvent → renderer → 替换 print/input 边界
   ↓                    ↓
streaming          tool/approval/status
   └──────────┬─────────┘
              ↓
       JSONL + persistent plan
              ↓
       task mode permission profiles
          ↙               ↘
       review             explain
          └──────┬─────────┘
                 ↓
      resume/replay/exit codes
                 ↓
        VS Code argv prototype
                 ↓
       matrix/docs/version/wheel
```

不要先写 VS Code extension 再反推 CLI 协议；也不要先做 spinner/面板再定义事件。前端必须消费已经稳定、可测试且脱敏的 CLI 事件。

## 8. 常见误区

### 8.1 把 SessionEvent 直接打印成 JSONL

Session payload 可能包含 prompt、tool output、diff 和 artifact 引用，不是公开 UI schema。应产生最小、脱敏的 UiEvent 投影。

### 8.2 为 streaming 重写 agent loop

agent 仍消费完整 normalized response。streaming 应封装在 model client 传输层，并最终返回相同对象。

### 8.3 部分流失败后自动发起第二次请求

这会重复成本，并可能产生不同工具调用。失败应进入现有 session 恢复语义，由用户 resume。

### 8.4 用提示词实现 review 只读

提示词不是权限边界。必须在发给模型的工具 schema 和 execute dispatch 两处使用同一 tool profile。

### 8.5 为计划创建独立 JSON 文件

独立文件会与 reducer/checkpoint 漂移。计划应是 SessionEvent 和 reducer state 的一部分。

### 8.6 把终端颜色写进业务测试

测试语义事件和少量 renderer token，不维护大段 golden ANSI snapshot。

### 8.7 VS Code 通过 `sendText()` 拼命令

文件名、task 和 workspace 可能包含 shell 元字符。使用 ProcessExecution 的 executable + args。

### 8.8 一开始就引入 full-screen TUI

全屏状态会带来终端恢复、尺寸变化、并发输出和 accessibility 成本。先让行式事件流稳定。

## 9. 调试与排错

### delta 显示了两次

检查完成后是否又打印了完整 response text。streaming 模式下 final event 只结束状态，不重复已经发出的文本。

### JSONL 被普通 banner 污染

确认 CLI 在创建任何 SessionStore/model client 前选定 renderer，并把所有 live 输出经过 UiEmitter。

### 审批提示与模型文本混行

approval.requested 前调用 renderer 的 line boundary + flush；不要在 approval handler 内自行维护 streaming flag。

### resume 后计划消失

检查 `plan.updated` 是否进入 codec、reducer、checkpoint 和 replay，而不是只保存在 UI 对象。

### review 仍能看到 run_command

检查发给模型的 TOOL_DEFINITIONS 是否按 task mode 过滤，并在 execute_tool 入口对 mode 再校验。

### Windows 编辑器命令参数错乱

确认 extension 使用 ProcessExecution 参数数组，没有 `join(" ")`、`shell=True` 或 `sendText()`。

## 10. M6 最终验收清单

- [x] M1-M5 的 640 项基线全部通过。
- [x] UiEvent schema、词表、seq、脱敏和 JSON round-trip 已固定。
- [x] agent/approval live 输出集中经过 UiEmitter。
- [x] TTY、non-TTY、NO_COLOR、JSONL 和 broken pipe 行为稳定。
- [x] streaming 最终文本与 normalized/durable response 一致。
- [x] partial stream 不自动重发请求。
- [x] tool、approval、verification、sandbox 和 recovery 状态可见但不泄密。
- [x] update_plan 进入 session、reducer、resume 和 replay。
- [x] run/review/explain 使用显式工具 profile。
- [x] review findings 结构化、位置有效、可 JSONL 输出。
- [x] explain 保持只读且引用已读取证据。
- [x] Ctrl+C 返回 130 并安全终止活动工作。
- [x] 旧 session 继续可读、可恢复（缺少 plan 时兼容为空计划）。
- [x] VS Code prototype 使用 argv ProcessExecution，无 shell 拼接。
- [x] 默认测试不要求真实模型、TTY、Docker、Node 或 VS Code。
- [x] README、总计划、版本和 wheel 验收完成。

## 11. 完成后的边界

M6 完成后，coding-agent 将拥有稳定的本地产品体验层：同一安全 agent loop 可以面向人类终端、JSONL 消费者和最小编辑器入口，任务计划与 review 结果可以恢复和审计。

仍需明确：line-oriented terminal 不是 full-screen TUI；VS Code prototype 不是后台服务；JSONL 不是远程协议；规则式 review 也不能替代人工安全审查。远程执行、多代理、插件、遥测、Web UI 和 Marketplace 发布应在后续里程碑基于真实需求单独设计。
