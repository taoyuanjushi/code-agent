# M4 阶段详细实现指南：会话持久化与可审计恢复

## 1. 阶段目标

M3 已经实现“发现验证命令 → 修改 → 验证失败 → 继续修复 → 验证通过”的单进程闭环。M4 要解决的是：进程退出后，任务状态不能恢复，模型请求、工具调用和审批决定也无法完整审计。

完成 M4 后，代理应具备以下能力：

```text
创建会话并追加 JSONL 事件
        ↓
记录提示词、模型响应、工具调用、审批与验证结果
        ↓
每个安全边界后保存可恢复检查点
        ↓
中断后加载最后一个有效检查点
        ↓
校验 workspace，避免重复执行有副作用的工具
        ↓
继续模型/工具循环，或离线回放完整时间线
```

M4 不负责完整 shell 沙箱、容器隔离、远程会话同步、数据库服务或 IDE 界面；这些属于 M5/M6。M4 的 `replay` 是离线查看历史，不是重新执行工具。

## 2. 学习目标与先修知识

建议先掌握：

- JSONL、UTF-8、事件日志、状态机和 reducer；
- `dataclass`、`Literal`、显式序列化与 schema 版本；
- 文件追加、`flush()`、`os.fsync()`、临时文件和 `os.replace()`；
- SHA-256、规范化 JSON，以及“完整性校验不等于防篡改”；
- 进程中断、幂等性、at-least-once 执行和未知执行结果；
- 当前调用链：`cli.py → agent.py → model_client.py/tools.py`；
- `VerificationToolState` 的发现结果、修复次数、编辑代次和验证历史。

开始前固定 M3 基线：

```powershell
.\.venv\Scripts\python.exe -m pytest -q --basetemp=.coding-agent\pytest-m4-baseline
```

## 3. 设计原则

### 3.1 JSONL 是事实源，检查点只是恢复缓存

每行只写一个完整事件，历史事件只追加、不原地修改。会话状态必须能够由事件重建；检查点用于缩短恢复过程，不能成为唯一审计依据。`index.json` 和审批汇总都是可重建投影，损坏时以 session JSONL 为准。

### 3.2 先落盘，再执行外部副作用

模型返回工具调用后，先记录调用计划和检查点，再执行工具。工具执行完成后，先记录结构化结果，再把结果发送给模型。这样可以区分“尚未开始”“已经完成”和“开始了但结果未知”。

### 3.3 resume 不等于盲目重试

读取和搜索可以安全重试；补丁、通用命令和验证命令可能产生副作用。若进程在这类工具执行期间中断，恢复时不得自动重复执行。补丁应比较文件前后哈希进行对账；命令结果未知时必须再次获得明确批准。

### 3.4 replay 必须零副作用

回放只读取和校验日志、构造时间线、输出摘要。它不能创建模型客户端，不能调用 `execute_tool()`，不能要求 `OPENAI_API_KEY`，也不能写 workspace。

### 3.5 不持久化秘密配置

仅保存经过白名单筛选的 `AgentConfig`。禁止记录 API key、完整环境变量和认证请求头。提示词和工具输出可能包含仓库内容，因此会话目录必须保持在已忽略的 `.coding-agent/` 中，并在文档中明确日志属于敏感本地数据。

## 4. 建议目录与领域模型

新增一个职责清晰的子包，避免继续扩大 `agent.py`：

```text
src/coding_agent/sessions/
  __init__.py
  models.py       事件、检查点、审批和 artifact 类型
  codec.py        显式 JSON 编解码、schema 校验和脱敏
  store.py        追加、加载、锁、尾部修复和 artifact 存储
  reducer.py      事件到 AgentSessionState 的纯函数归约
  replay.py       离线时间线和机器可读输出
tests/
  test_session_models.py
  test_session_store.py
  test_session_reducer.py
  test_session_resume.py
  test_session_replay.py
  test_approval_audit.py
  test_m4_integration.py
```

workspace 内的运行数据：

```text
.coding-agent/
  sessions/<session-id>.jsonl
  artifacts/<session-id>/<sha256>.blob
  locks/<session-id>.lock
  approvals.jsonl               # 可选、可重建的审批投影
  index.json                    # 可选、可重建的会话列表投影
```

事件公共信封固定为：

```json
{
  "schema_version": 1,
  "session_id": "20260714T031500Z-a1b2c3d4",
  "seq": 7,
  "event_id": "01...",
  "recorded_at": "2026-07-14T03:15:04.125Z",
  "type": "tool.finished",
  "prev_hash": "...",
  "payload": {},
  "event_hash": "..."
}
```

建议先定义：

```python
SessionStatus = Literal["running", "completed", "failed", "interrupted"]
SessionPhase = Literal[
    "awaiting_initial_model",
    "awaiting_tools",
    "awaiting_model",
    "finalizing",
    "completed",
]
ToolEffect = Literal["read_only", "workspace_write", "process"]

@dataclass(frozen=True)
class ArtifactRef:
    sha256: str
    byte_count: int
    media_type: str
    encoding: str | None

@dataclass(frozen=True)
class SessionEvent:
    schema_version: int
    session_id: str
    seq: int
    event_id: str
    recorded_at: str
    type: str
    prev_hash: str | None
    payload: dict[str, object]
    event_hash: str

@dataclass(frozen=True)
class PendingToolCall:
    call_id: str
    name: str
    arguments: str
    effect: ToolEffect
    started: bool

@dataclass(frozen=True)
class AgentSessionState:
    session_id: str
    task: str
    phase: SessionPhase
    turn_index: int
    previous_response_id: str | None
    pending_tool_calls: tuple[PendingToolCall, ...]
    pending_tool_outputs: tuple[dict[str, object], ...]
    completed_call_ids: frozenset[str]
    verification_state: dict[str, object]
    touched_file_hashes: dict[str, str | None]
```

不要用 `pickle`，也不要直接持久化 OpenAI SDK 对象。所有字段都要转换成稳定、可验证的 JSON 类型。

## 5. 详细实现步骤

### 5.1 第一步：先固定 M4 验收指标

先新增测试骨架，不要先改生产代码。把以下指标固定在测试中：

1. 一个新任务会在第一次模型调用前创建 session，并立即打印 session ID。
2. JSONL 每行都是 UTF-8 JSON；`seq` 从 1 连续递增，`event_hash` 链可验证。
3. 每个模型请求/响应、工具开始/结束、审批决定、验证结果和最终回答都可追踪。
4. 日志尾部只有半行时可以恢复到最后一个完整事件；中间行损坏必须拒绝加载。
5. fake client 在第一次补丁后模拟进程中断，resume 后补丁不会重复应用，并能继续验证到通过。
6. 未完成的 `run_command`/`run_verification` 不会因 `auto_approve_commands=True` 而静默重跑。
7. replay 在未设置 API key 时可运行，且模型调用数和工具调用数均为 0。
8. resume 到另一个 workspace、session ID 路径穿越或关键文件漂移均被拒绝。
9. API key 和敏感环境变量值不会出现在 session、artifact 或审批投影中。
10. M1-M3 完整测试继续通过。

建议将以下限制写成常量并固定测试：

```text
schema version                 1
inline payload maximum         64 KiB
single artifact maximum        4 MiB
session ID random suffix       8 hex characters or stronger
session writer count           1
```

超限内容写入 artifact；不能既截断又声称保存了完整审计数据。

> 实现状态：第一步已完成。`tests/test_m4_acceptance.py` 不导入尚未实现的 session 生产模块，而是以 11 个合同测试固定数值预算、JSONL 事件信封与 hash chain、首次模型调用前的 session 创建和 ID 输出顺序、审计事件、日志损坏处理、补丁中断恢复、进程工具重新审批、零副作用 replay、安全拒绝、敏感值边界以及 M1-M3 回归范围。该状态只表示验收目标已经冻结，不表示会话持久化功能已经实现。

### 5.2 第二步：定义事件、检查点和审批领域模型

在 `sessions/models.py` 中定义不可变数据类型和 `Literal`。至少覆盖：

- `session.started`、`session.resumed`、`session.completed`、`session.failed`；
- `context.created`；
- `model.requested`、`model.responded`；
- `tool.started`、`tool.finished`、`tool.recovered`；
- `approval.decided`；
- `checkpoint.saved`；
- `session.interrupted`。

`session.started` 保存任务、规范化 workspace、白名单配置、Git HEAD（若存在）和初始 workspace guard。`model.responded` 只保存规范化响应：response ID、文本、reasoning summary 和函数调用，不保存 SDK 实例。

审批模型建议为：

```python
ApprovalOutcome = Literal["approved", "denied"]
ApprovalSource = Literal["interactive", "auto_policy", "resume_recovery"]

@dataclass(frozen=True)
class ApprovalDecision:
    approval_id: str
    call_id: str
    action: str
    summary: str
    outcome: ApprovalOutcome
    source: ApprovalSource
    decided_at: str
    arguments_sha256: str
```

测试所有构造器边界：空 ID、非法状态、负序号、非 UTC 时间、绝对 artifact 路径和未知 schema version 均应失败。

> 实现状态：第二步已完成。新增 `src/coding_agent/sessions/models.py` 和包导出，定义 session/event/phase/status、工具副作用、审批结果与来源等 `Literal`，并提供 `SessionEvent`、`ArtifactRef`、`WorkspaceGuard`、`SessionStarted`、`NormalizedModelResponse`、`PendingToolCall`、`AgentSessionCheckpoint` 和 `ApprovalDecision` 等不可变模型。所有 JSON 领域字段会深度冻结，session 配置只接受安全白名单；构造期会拒绝未知 schema/event、非法 ID、非 UTC 时间、错误 SHA-256、非规范路径、重复或冲突 call ID 及非法 phase 状态。`tests/test_session_models.py` 覆盖上述正常与失败边界。本步骤尚未实现 codec、hash 计算、日志存储或 resume。

### 5.3 第三步：实现显式 JSON codec 和 schema 演进边界

在 `sessions/codec.py` 中为每个领域对象写 `to_dict()`/`from_dict()`。不要依赖 `dataclasses.asdict()` 自动完成协议，因为 tuple、frozenset、Literal 和后续迁移都需要明确处理。

实现顺序：

1. 只接受 `schema_version == 1`；未知版本返回包含文件和行号的错误。
2. JSON 输出使用 `ensure_ascii=False`、稳定 key 顺序和紧凑分隔符。
3. 计算 `event_hash` 时排除自身，使用规范化 JSON 字节和 SHA-256。
4. 下一事件的 `prev_hash` 必须等于上一事件的 `event_hash`。
5. 为 `VerificationCommand`、`VerificationDiscoveryResult`、`VerificationResult` 和 `VerificationToolState` 增加显式 round-trip codec。
6. 解码时重新调用领域校验，不信任磁盘数据。

迁移接口先预留但不实现虚假兼容：

```python
def decode_event(raw: bytes) -> SessionEvent: ...
def migrate_event(data: dict[str, object], from_version: int) -> dict[str, object]: ...
```

V1 阶段遇到未知版本应清晰失败，不能猜测字段。

> 实现状态：第三步已完成。新增 `sessions/codec.py`，为全部 session 领域模型以及 `VerificationCommand`、`VerificationDiscoveryResult`、`VerificationResult`、`VerificationToolState` 提供显式 `to_dict()`/`from_dict()`。事件使用 UTF-8、稳定键顺序和紧凑分隔符生成规范化 JSON；`event_hash` 排除自身后计算 SHA-256，并校验连续序号、session ID、`prev_hash` 和事件内容。解码会严格拒绝缺失/未知字段、损坏 UTF-8/JSON 和未知 schema version，错误包含来源与行号，并重新执行领域构造校验。`tests/test_session_codec.py` 覆盖 Unicode、所有模型 round-trip、M3 验证恢复状态和事件链损坏场景。`SessionStore`、JSONL 文件追加、并发锁、尾部修复和 artifact I/O 不属于本步骤，由第四步实现。

### 5.4 第四步：实现安全的 SessionStore

在 `sessions/store.py` 中集中管理路径和 I/O：

```python
class SessionStore:
    def create(self, started_payload: dict[str, object]) -> str: ...
    def append(self, session_id: str, event_type: str, payload: dict[str, object]) -> SessionEvent: ...
    def load(self, session_id: str, *, repair_tail: bool = False) -> tuple[SessionEvent, ...]: ...
    def list_sessions(self) -> tuple[SessionSummary, ...]: ...
    def put_artifact(self, session_id: str, content: bytes, media_type: str) -> ArtifactRef: ...
    def get_artifact(self, session_id: str, ref: ArtifactRef) -> bytes: ...
```

必须做到：

1. session ID 使用固定正则校验，再拼接路径；任何 `/`、`\`、`..` 都拒绝。
2. 所有路径经过 `resolve_inside_workspace()`；`.coding-agent` 继续由统一忽略策略排除。
3. append 以二进制模式写“一行 + `\n`”，随后 `flush()` 和 `os.fsync()`。
4. artifact 先写同目录临时文件，fsync 后 `os.replace()`；文件名由内容 SHA-256 决定。
5. 使用进程级独占锁保证单 writer。Windows 可封装 `msvcrt.locking`，POSIX 使用 `fcntl.flock`；锁随文件描述符关闭而释放。
6. 读取时验证 UTF-8、连续序号、session ID、hash chain 和 artifact hash。
7. 仅允许修复“最后一行未完成”；中间坏行、序号回退或 hash 不匹配必须报错并保持原文件不变。

尾部修复前先把原始尾部复制为诊断 artifact，再截到最后一个有效换行，最后追加 `session.resumed` 说明发生过修复。

> 实现状态：第四步已完成。新增 `src/coding_agent/sessions/store.py`，按 `.coding-agent/sessions/<id>.jsonl`、`artifacts/<id>/<sha256>.blob` 和 `locks/<id>.lock` 管理会话数据。所有 session ID 会在路径构造前按固定正则验证，所有存储路径均通过 `resolve_inside_workspace()` 限定；事件以二进制 JSONL 追加并执行 `flush()`/`fsync()`。Windows 使用 `msvcrt.locking`、POSIX 使用 `fcntl.flock`，并结合进程内锁表非阻塞拒绝第二个 writer。加载会验证 UTF-8、schema、session ID、连续序号和 hash chain；仅不完整尾行可显式修复，修复前保存诊断 artifact，随后截断并追加 `session.resumed`。Artifact 使用 SHA-256 内容寻址、同目录临时文件、`fsync()` 和 `os.replace()` 原子落盘，读取时复核路径、字节数和哈希。`SessionSummary` 和 `list_sessions()` 也已实现。`tests/test_session_store.py` 覆盖路径注入、完整性损坏、尾部修复、原子写失败、同进程及跨进程 writer 冲突等 23 个场景。敏感信息过滤和 payload 大小策略现已由第五步实现；reducer 已由第六步实现，agent/CLI 接入仍属于后续步骤。

### 5.5 第五步：实现敏感信息过滤和大字段 artifact 化

在进入 store 前统一执行持久化策略，不要让各调用点自行决定：

```python
@dataclass(frozen=True)
class SessionPrivacyPolicy:
    inline_max_bytes: int = 65_536
    artifact_max_bytes: int = 4_194_304

    def sanitize_config(self, config: AgentConfig) -> dict[str, object]: ...
    def sanitize_payload(self, value: object) -> object: ...
```

规则至少包括：

- 配置只保存 workspace、model、reasoning effort、turn/fix/context 限制和权限模式；
- 永远不枚举或保存完整 `os.environ`；
- 对当前进程中已知的 `OPENAI_API_KEY`、`*_TOKEN`、`*_SECRET`、`*_PASSWORD` 值做精确替换；
- 请求头、认证对象和异常对象先转换成安全摘要；
- 大提示词、diff、工具输出和 traceback 写 artifact，事件只保存 `ArtifactRef` 与短摘要；
- artifact 超过硬限制时明确记录 `stored=false`、原字节数和原因。

不要声称正则可以发现所有源码中的秘密。README 应提醒用户：session 包含任务、代码片段和命令输出，不应随意上传。

> 实现状态：第五步已完成。新增 `src/coding_agent/sessions/privacy.py`，实现默认 64 KiB inline 上限和 4 MiB artifact 硬上限。`SessionPrivacyPolicy` 使用显式配置白名单，不持久化完整 `os.environ`，并对当前进程中已知的 `OPENAI_API_KEY`、`*_TOKEN`、`*_SECRET`、`*_PASSWORD` 值执行精确替换；请求头、认证对象、异常和不支持对象只保留安全摘要。大文本、diff、工具输出、traceback、二进制值以及超过总 inline 预算的 payload 会转换为内容寻址 artifact；超过硬上限或缺少 artifact writer 时记录 `stored=false`、原字节数和原因。`SessionStore` 默认强制应用该策略，直接 artifact 写入和尾部修复诊断数据也会再次过滤，并保留 UTF-8 encoding 元数据。`tests/test_session_privacy.py` 覆盖配置白名单、环境隔离、字段过滤、异常/认证摘要、文本与二进制 artifact、总 payload 预算、硬上限、直接写入和尾部修复。README 已明确 session 目录仍可能包含代码和命令输出，过滤策略不能替代 secret scanning 或人工检查。

### 5.6 第六步：用纯 reducer 重建会话状态

在 `sessions/reducer.py` 实现：

```python
def reduce_event(state: AgentSessionState | None, event: SessionEvent) -> AgentSessionState: ...
def rebuild_state(events: tuple[SessionEvent, ...]) -> AgentSessionState: ...
```

reducer 不读文件、不调用模型、不执行工具，只检查合法状态转换。例如：

```text
session.started
  → awaiting_initial_model
model.responded(calls != [])
  → awaiting_tools
所有 tool.finished
  → awaiting_model
model.responded(calls == [])
  → finalizing
session.completed
  → completed
```

`checkpoint.saved` 保存完整可恢复状态，包括：当前 phase、turn、response ID、尚未处理的 tool calls、已经完成的 call IDs、待发送 tool outputs、`VerificationToolState` 和 touched file hashes。加载时用事件重建结果校验 checkpoint；两者冲突时拒绝 resume，而不是信任最新对象。

重点测试：相同事件序列始终得到相同状态；重复 `tool.finished`、未 `tool.started` 就结束、completed 后继续追加业务事件等非法转换均失败。

> 实现状态：第六步已完成。新增 `AgentSessionState`、`SessionReductionError`、`reduce_event()` 和 `rebuild_state()`。reducer 仅根据不可变事件推进 phase/status，验证 session ID、连续序号、前序哈希、模型请求/响应、工具开始/完成、审批、验证记录和终结状态；不会读取 artifact、文件或调用模型/工具。`checkpoint.saved` 只用于校验事件重建结果，字段冲突会拒绝恢复。M3 `VerificationToolState`、pending calls/outputs、completed call IDs、审批决定和 touched file hashes 均可确定性恢复。`tests/test_session_reducer.py` 覆盖完整两轮模型循环、纯函数不变性、checkpoint 冲突、未知/重复 call ID、工具恢复、终结后追加、artifact 控制字段和验证状态 round-trip。

### 5.7 第七步：重构 agent 循环并接入模型事件

当前 `agent.py` 依赖内存中的 response 对象。先把它重构为可暂停状态机，再接入持久化，避免一边写日志一边大改流程。

推荐顺序：

1. 新增 `NormalizedModelResponse`，复用 `_get_response_id()`、`_find_function_calls()` 和文本提取逻辑，将 SDK response 立即规范化。
2. 把“初始模型调用”“执行工具批次”“继续模型调用”“生成最终报告”拆成小函数。
3. 新会话在收集初始上下文之前写 `session.started`；上下文完成后写 `context.created`，大内容放 artifact。
4. 每次调用模型前写 `model.requested`，返回后写 `model.responded` 和 checkpoint。
5. 工具结果批次先完整持久化，再调用 `create_tool_response()`。
6. 最终文本和 `AgentRunReport` 落盘成功后，才能写 `session.completed` 并向 CLI 返回。
7. `KeyboardInterrupt`、turn limit 和普通异常分别写 `session.interrupted` 或 `session.failed`；保留原异常语义。

模型请求在服务端成功、但本地尚未记录响应时崩溃，resume 可能需要重新请求并产生额外费用。M4 必须在文档和事件中如实标记这种窗口；不能把它描述成 exactly-once。工具副作用才是必须重点去重的部分。

> 实现状态：第七步已完成。`model_client.py` 新增 `normalize_model_response()`，模型返回后立即把 SDK 对象或字典转换为可持久化的 `NormalizedModelResponse`；`agent.py` 已拆分会话启动、初始请求、工具批次、继续请求、响应落盘和最终报告等步骤。新任务会在收集上下文前写入 `session.started` 并输出 session ID，随后按顺序记录 `context.created`、`model.requested`、`model.responded`、`checkpoint.saved`、工具与验证事件。完整工具批次先持久化并保存 checkpoint，再发送给模型；大结果和最终 `AgentRunReport` 使用 artifact 保存，只有报告落盘成功后才写 `session.completed`。`KeyboardInterrupt`、turn limit 和普通异常会记录终结事件并重新抛出原异常。`model.requested.delivery_semantics` 明确标记为 `at_least_once_after_unrecorded_response`，不承诺 exactly-once。`tests/test_agent_sessions.py` 覆盖规范化、事件顺序、批次持久化、artifact、失败、中断和 turn limit。工具 effect 映射仍暂存在 `agent.py`，将在第八步集中。

### 5.8 第八步：集中工具元数据和审批审计

为所有工具建立唯一注册表，至少包含名称、effect 和是否需要审批：

```python
TOOL_POLICIES = {
    "read_many_files": ToolPolicy(effect="read_only", approval=False),
    "search_text": ToolPolicy(effect="read_only", approval=False),
    "apply_patch": ToolPolicy(effect="workspace_write", approval=True),
    "run_verification": ToolPolicy(effect="process", approval=True),
    "run_command": ToolPolicy(effect="process", approval=True),
}
```

将 `tools.py` 内分散的 `input()` 抽成统一回调：

```python
ApprovalHandler = Callable[[ApprovalRequest], ApprovalDecision]
```

执行顺序必须为：

```text
tool.started（含参数摘要/哈希）
    ↓
展示 diff 或 argv/cwd
    ↓
approval.decided（先持久化）
    ↓
真正执行副作用
    ↓
tool.finished（ToolResult + data）
    ↓
checkpoint.saved
```

自动批准也要记录，`source="auto_policy"`；拒绝、输入异常和审批回调异常同样记录。审批投影可以从所有 `approval.decided` 事件重建，避免把 `approvals.jsonl` 当第二事实源。

为 `apply_patch` 的结构化结果增加 changed paths、diff hash、每个目标文件的 before/after SHA-256。命令审计保存实际 argv/cwd、超时和退出状态；不得只保存面向模型的文本摘要。

> 实现状态：第八步已完成。新增 `tool_policy.py` 作为工具名称、effect、审批分组和暴露状态的唯一注册表，并通过测试保证它与工具 schema 一致；新增 `approvals.py` 统一审批请求、决定、默认交互回调和参数绑定校验。`agent.py` 现在按 `tool.started → approval.decided → tool.finished → checkpoint.saved` 顺序落盘，自动批准、拒绝、回调异常和非法决定均可审计。补丁结果记录 changed paths、diff hash、文件 before/after SHA-256，命令与验证结果记录实际 cwd、argv/command、shell、超时、耗时和退出状态。reducer 会拒绝错误动作、错误参数哈希、重复决定及 request/decision 不一致。相关回归测试见 `tests/test_approval_audit.py` 和 `tests/test_session_reducer.py`。

### 5.9 第九步：实现中断工具的安全对账与去重

resume 先查找最后一个没有对应 `tool.finished` 的 `tool.started`：

- `read_only`：允许自动重试，并写 `tool.recovered`，原因是 `safe_retry`。
- `apply_patch`：比较记录的 before/after hashes。
  - 全部等于 after：认为补丁已落地，构造恢复结果，不再次应用；
  - 全部等于 before：补丁尚未生效，可重新展示 diff 并审批；
  - 混合或都不匹配：标记 workspace drift，拒绝继续。
- `run_command`/`run_verification`：结果视为 unknown。即使原会话开启自动批准，也必须交互批准或显式 recovery 选项后才可重跑，并记录 `source="resume_recovery"`。

同一个 `call_id` 一旦存在完成结果，后续重复出现时直接复用已持久化的 function-call output，绝不能再次调用 `execute_tool()`。恢复发送给模型的 output 必须与日志中保存的 JSON 完全一致。

测试要在 `execute_tool()` 返回后、写 `tool.finished` 前注入崩溃，而不是只在工具执行前抛异常；只有这样才能真正验证副作用去重。

> 实现状态：第九步已完成。新增 `sessions/recovery.py`，可从事件与 reducer 状态定位已开始但未完成的调用，并按 `read_only`、`apply_patch` 和 `process` 生成恢复计划。补丁审批事件会在副作用前持久化 diff hash 与每个文件的 before/after SHA-256；恢复时全 after 直接构造 `tool.recovered` 完成结果，全 before 重置为待重试并要求重新审批，混合或未知 hash 返回包含期望值与当前值的 workspace drift。已完成 call ID 通过日志复用原始 function-call output。进程工具恢复审批使用 `source="resume_recovery"`，默认自动批准不能复用；reducer 只允许一次受控恢复决定，并可校验 `recovery_retry`。代理新增测试专用 fault injector，测试在 `execute_tool()` 返回后、`tool.finished` 前真实中断。共享的 `tool_outputs.py` 保证正常完成与恢复完成采用相同的 artifact 和 JSON 序列化策略。相关测试见 `tests/test_tool_recovery.py`。

### 5.10 第十步：实现 workspace guard 和 resume

新增 `resume_agent(session_id, workspace, model_client=None)`。恢复前依次检查：

1. session 日志和 hash chain 有效；
2. session 记录的规范化 workspace 与当前 workspace 一致；
3. Git HEAD（若有）和关键路径状态没有不可解释漂移；
4. 会话触碰过的每个文件与最新 checkpoint 的 hash 一致；
5. session 尚未 completed，且剩余 turn/fix 限制有效；
6. 没有另一个 writer 持有同一 session 锁。

恢复后根据 phase 分派：

- `awaiting_initial_model`：重新发初始请求并注明可能重复计费；
- `awaiting_tools`：完成对账后继续尚未完成的调用；
- `awaiting_model`：把已保存的 tool outputs 和 previous response ID 继续发送；
- `finalizing`：从已保存模型文本生成报告并完成 session；
- `completed`：拒绝 resume，提示使用 replay。

`VerificationToolState` 必须完整恢复，包括 discovery、verification history、未解决失败命令、repair attempts、edit generation 和 passed generations。否则 M3 的修复上限和“编辑后验证失效”会被绕过。

如果 Responses API 不再接受保存的 `previous_response_id`，M4 应返回明确的 `resume_model_context_unavailable`，保留 session 可 replay；不要静默创建一个缺少历史的新对话。重新注入完整历史可作为后续增强。

> 实现状态：第十步已完成。新增 `sessions/workspace_guard.py`，恢复前会校验规范化 workspace、Git HEAD、已触碰文件 SHA-256，并允许由中断补丁前后哈希解释的受控状态转换。`agent.py` 新增 `resume_agent()` 和 `resume_agent_with_report()`，按 `awaiting_initial_model`、`awaiting_tools`、`awaiting_model`、`finalizing` 分派恢复，复用已完成工具输出，恢复 `VerificationToolState`，并对进程型恢复使用独立审批。`SessionStore.exclusive_writer()` 在整个恢复期间持有单 writer 租约；完成会话、漂移、超限和模型上下文失效均返回明确错误。相关覆盖见 `tests/test_agent_resume.py`、`tests/test_session_store.py` 和 `tests/test_session_reducer.py`。

### 5.11 第十一步：增加 CLI 会话命令

保持现有调用兼容：

```powershell
python -m coding_agent "修复失败测试"
python -m coding_agent --resume 20260714T031500Z-a1b2c3d4
python -m coding_agent --resume latest
python -m coding_agent --replay 20260714T031500Z-a1b2c3d4
python -m coding_agent --replay 20260714T031500Z-a1b2c3d4 --json
python -m coding_agent --list-sessions
```

实现要点：

1. 将 task 从 `nargs="+"` 改为 `nargs="*"`，然后做语义校验。
2. 新任务、resume、replay、list 四种模式互斥。
3. replay/list 在 API key 检查之前执行，也不构造 `OpenAIResponsesClient`。
4. 新任务在任何远程调用前打印 session ID，便于 Ctrl+C 后恢复。
5. resume 默认采用 session 中的安全配置；CLI 不得静默把 read-only 提升为 workspace-write。
6. `latest` 按事件时间和 session ID 稳定选择，并且只在当前 workspace 中查找。
7. `--json` 输出稳定 schema 到 stdout，诊断信息写 stderr，便于脚本消费。

如果加入 `--no-session`，必须是显式隐私选项，并在输出中说明该运行不可恢复、不可完整审计；默认仍应持久化。

> 实现状态：第十一步已完成。`cli.py` 支持新任务、`--resume`、`--replay`、`--list-sessions` 和 `--approvals` 互斥模式；只读查询在 API key 校验前执行，resume 拒绝 `--write` 等新任务配置覆盖。`sessions/query.py` 保留 workspace 内 `latest` 稳定选择和会话列表投影；replay 与审批查询由第十二步的 `sessions/replay.py` 提供。相关覆盖见 `tests/test_cli_sessions.py`。

### 5.12 第十二步：实现只读 replay 和审批查询

`replay.py` 从事件构造时间线，默认只显示摘要：

```text
session started
model requested → response r_...
tool apply_patch → approved(interactive) → ok
verification python:pytest → failed
model requested → response r_...
tool apply_patch → approved(auto_policy) → ok
verification python:pytest → passed
session completed → final_status=passed
```

默认不要直接打印完整源码、diff 和命令日志；`--verbose` 才读取 artifact。`--json` 返回结构化 timeline、最终状态、验证历史和审批列表。

为 replay 加硬性副作用测试：monkeypatch 模型客户端构造器、`execute_tool()`、`subprocess.run()` 和 `input()`，只要任意一个被调用就让测试失败。缺失 artifact 应显示明确占位信息；事件本身损坏则拒绝回放。

审批查询至少支持按 session、action 和 outcome 过滤。查询以 session 事件为准，`approvals.jsonl` 若存在只作为加速投影，并提供重建函数。

> 实现状态：第十二步已完成。`SessionStore(read_only=True)` 在构造、加载、读取 artifact 和列出会话时不创建目录、锁或修复尾部，所有写入 API 与 writer lease 都会被明确拒绝。新增 `sessions/replay.py`，提供 schema version 2 的摘要时间线、最终状态、验证历史和审批列表；默认不暴露完整 diff、工具输出或验证日志，`--verbose` 才校验并展开 artifact，缺失 artifact 返回 `artifact_missing` 占位。审批查询支持 `--approvals [all|latest|SESSION_ID]`、`--approval-action` 和 `--approval-outcome`，并始终从 `approval.decided` 事件重建事实。离线测试固定模型、工具、subprocess、输入调用数和 workspace 写入数都为 0，覆盖见 `tests/test_session_replay.py` 与 `tests/test_cli_sessions.py`。

### 5.13 第十三步：补齐测试矩阵

建议覆盖：

1. **模型与 codec**：Unicode、tuple/frozenset round-trip、未知字段/版本、非法状态。
2. **store**：连续追加、两个 writer 冲突、半行尾部修复、中间损坏拒绝、artifact 原子写和 hash 校验。
3. **完整性**：修改历史事件、调换事件顺序、复制其他 session 的事件均被检测。
4. **脱敏**：API key、token、password 不出现在任何落盘字节中；大 payload 变为 artifact。
5. **reducer**：每种 phase、非法转换、checkpoint 与重建状态不一致。
6. **审批**：交互批准、拒绝、自动批准、恢复重试和异常路径全部有审计事件。
7. **状态恢复**：M3 的 verification discovery/history、repair limit、edit generation 精确 round-trip。
8. **工具去重**：已完成 call ID 不重复；读取工具可重试；补丁 after-hash 对账；未知命令结果不自动重跑。
9. **workspace guard**：不同根目录、Git HEAD 改变、已触碰文件外部修改、session ID 注入。
10. **CLI**：五种模式互斥；replay/list/approvals 无 task、无 API key 也可执行；completed session 不可 resume。
11. **离线 replay**：模型、工具、subprocess 和输入函数调用数都为 0。
12. **端到端**：中型仓库经历失败验证、补丁、中断、resume、再次验证通过，补丁只应用一次。

可使用测试专用故障注入点：

```python
FaultPoint = Literal[
    "after_model_response",
    "after_tool_side_effect",
    "after_tool_finished",
    "before_model_continuation",
]
```

故障注入只进入测试依赖，不要通过环境变量隐藏在生产逻辑中。

> 实现状态：第十三步已完成。现有 codec、store、完整性、隐私、reducer、审批、验证状态、工具恢复、workspace guard、CLI 和离线 replay 测试覆盖矩阵前 11 项；新增 `tests/test_m4_integration.py` 参数化覆盖全部四个 `FaultPoint`，固定只读工具在 `after_tool_side_effect` 后安全重试、其他边界不重复执行。中型仓库测试覆盖“首次验证失败 → 补丁副作用完成后中断 → resume 按 after-hash 对账 → 第二次验证通过”，并断言补丁仅应用一次、无关文件未变化、审批/验证/恢复事件完整、reducer 与只读 replay 最终状态一致。

### 5.14 第十四步：更新文档并完成验收

完成代码后同步：

- `README.md`：新增 session/resume/replay 命令、敏感数据提示和项目结构；
- `docs/implementation-plan.md`：将 M4 标为已完成，但只能在验收全部通过后更新；
- `.env.example`：仅在新增非秘密配置时添加安全默认值；
- 包版本：按项目版本策略决定是否从 `0.2.x` 升到 `0.3.0`。

建议验收命令：

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_session_models.py tests\test_session_store.py -q
.\.venv\Scripts\python.exe -m pytest tests\test_agent_resume.py tests\test_session_replay.py tests\test_m4_integration.py -q
.\.venv\Scripts\python.exe -m pytest -q --basetemp=.coding-agent\pytest-m4-full
.\.venv\Scripts\python.exe -m pip wheel . -w dist
git diff --check
```

构建隔离需要可用的包索引。离线环境中，确认构建依赖已安装后，可追加 `--no-deps --no-build-isolation`，避免构建过程尝试下载依赖。

> 实现状态：第十四步已完成。包版本已从 `0.2.0` 更新为 `0.3.0`。核心会话测试 63 项、恢复/回放/集成测试 24 项、全量测试 406 项全部通过，`compileall` 与 `git diff --check` 通过。离线构建使用已安装的 setuptools 82.0.1，生成 `dist/coding_agent-0.3.0-py3-none-any.whl`（101003 字节、36 个文件，SHA-256 `600268e16891cb2cab77d56d225e0facf4b172455805d588569e3a45dc0d6408`）。wheel 已确认不包含 `tests/`、`.env` 或 `.coding-agent/`，并通过隔离目录安装、`coding_agent.__version__`、`SessionStore` 导入、CLI parser 和 `coding-agent --help` 返回码 0 的 smoke 验证。README、总实施计划和本指南已同步，M4 验收完成。

## 6. 推荐事件序列示例

一次“失败后修复并通过”的正常会话应近似为：

```text
1  session.started
2  context.created
3  model.requested
4  model.responded                 # discover + search + read + patch
5  checkpoint.saved                # awaiting_tools
6  tool.started(discover)
7  tool.finished(discover)
8  checkpoint.saved
...
15 tool.started(apply_patch)
16 approval.decided(approved)
17 tool.finished(apply_patch)
18 checkpoint.saved
19 model.requested                  # 发送工具结果批次
20 model.responded                  # 请求 run_verification
21 checkpoint.saved
22 tool.started(run_verification)
23 approval.decided(approved)
24 tool.finished(failed)
25 checkpoint.saved
...
40 tool.finished(run_verification, passed)
41 checkpoint.saved
42 model.requested
43 model.responded                  # 最终回答，无工具调用
44 checkpoint.saved                # finalizing
45 session.completed
```

如果在第 17 行副作用已经发生但事件还没写入就崩溃，日志会停在 `tool.started`/`approval.decided`。resume 必须通过文件哈希判断补丁已应用，再追加 `tool.recovered`，而不是再次应用 diff。

## 7. 常见误区

### 7.1 每轮覆盖一个 `session.json`

崩溃可能留下空文件或半个对象，也失去历史审计信息。使用 append-only JSONL，并对尾部损坏做严格处理。

### 7.2 直接序列化 SDK response

SDK 类型和字段可能随版本变化，也可能包含不可 JSON 化或不应落盘的对象。应立即规范化成项目自己的模型。

### 7.3 把 previous response ID 当完整备份

它只是服务端对话引用，不保证永久可用。session 仍要保存模型文本、工具调用和发送过的 tool outputs；M4 无法恢复时要明确报错。

### 7.4 resume 后重新运行所有工具

这会重复打补丁、执行脚本或产生外部副作用。恢复必须按 call ID 去重，并区分 read-only、workspace-write 和 process。

### 7.5 只记录“用户点了 yes”

审批必须关联 call ID、动作摘要、参数哈希、决定来源和时间。自动批准与拒绝也属于审计事实。

### 7.6 replay 调用真实工具来“还原”过程

那是 re-execution，不是 replay。M4 replay 只能读取历史；将来如果实现重新执行，必须是独立命令和更严格沙箱。

### 7.7 在日志中保存完整环境变量

环境中通常包含 API key、代理凭据和云令牌。只保存配置白名单，永远不要为了“可复现”记录整个环境。

## 8. 调试与排错

### session 无法加载

先报告失败行号和类型，再区分：尾部半行、UTF-8 错误、hash chain 错误、未知 schema。只有尾部半行允许自动修复，其他情况保留原文件并停止。

### resume 提示 workspace drift

输出发生变化的相对路径、期望 hash 和实际 hash，不要自动覆盖。用户确认仓库状态后新建 session，或在未来实现显式的 drift override 审计流程。

### resume 重复调用模型

检查最后一个 `model.requested` 是否有对应 `model.responded`。请求已发出但响应未持久化时只能安全重试远程调用，并告知可能重复计费；不能伪造响应。

### completed session 仍显示 running

检查 `model.responded`、finalizing checkpoint 和 `session.completed` 的写入顺序。若最终响应已持久化，可以离线补写完成事件；不要再次请求模型。

### replay 意外要求 API key

CLI 的 API key 检查位置错误。list/replay/approvals 应在模型配置和客户端初始化之前完成，并使用严格只读 store。

## 9. 面试会怎么问

### 问：为什么选择 JSONL 而不是单个 JSON？

答：JSONL 适合追加式事件，每次只写一行，崩溃时通常只影响尾部；它易于流式读取、审计和后续导入数据库。单个大 JSON 每次都要整体重写，恢复窗口更大。

### 问：事件日志和检查点分别有什么作用？

答：事件日志记录不可变事实，是审计和重建依据；检查点保存归约后的运行状态，用于快速恢复。检查点损坏时应能由事件重建，而不是反过来。

### 问：为什么不能保证 exactly-once？

答：进程可能在外部副作用成功后、完成事件落盘前崩溃，本地无法通过一次写操作同时提交外部效果和日志。系统通过 call ID、文件哈希对账和人工批准实现 effectively-once，而不是虚假承诺 exactly-once。

### 问：hash chain 能防止恶意篡改吗？

答：不能。没有外部签名密钥或可信存储时，攻击者可以重算整条链。它主要检测偶发损坏和不一致，提升审计可靠性，但不提供强真实性证明。

### 问：resume 为什么要恢复 VerificationToolState？

答：它包含修复次数、未解决失败、编辑代次和已通过验证的代次。如果只恢复聊天上下文，代理可能绕过修复上限，或把编辑前的通过结果误当成当前有效结果。

### 问：replay 和 retry 有什么区别？

答：replay 只读取过去的事件并展示，不产生副作用；retry 会再次调用模型或工具，必须经过恢复策略、幂等检查和审批。

## 10. M4 最终验收清单

- [x] 新任务默认创建 `.coding-agent/sessions/<id>.jsonl`，并在远程调用前显示 ID。
- [x] 事件 schema、连续序号、UTF-8、hash chain 和 artifact hash 均有校验。
- [x] 提示、模型响应、工具调用/结果、审批、验证和最终回答可审计。
- [x] API key、完整环境变量和认证头不会落盘。
- [x] `VerificationToolState` 可无损序列化并在 resume 后继续执行限制。
- [x] 尾部半行可修复，中间损坏和未知版本被拒绝。
- [x] read-only 工具可安全重试，已完成 call ID 不会重复执行。
- [x] 中断后的补丁能通过前后哈希对账，不会重复应用。
- [x] 结果未知的进程工具不会被自动重跑。
- [x] workspace 不一致、关键文件漂移和并发 writer 会阻止 resume。
- [x] replay/list/approvals 不需要 API key，不调用模型、工具、subprocess 或输入函数，并且不写入 workspace。
- [x] 全部四个故障注入点、中型仓库中断恢复和 M1-M3 回归测试均纳入最终矩阵。
- [x] `0.3.0` wheel 的元数据、内容、隔离安装、包导入和控制台入口 smoke 均通过。
- [x] README、总实施计划和本指南已同步到第十四步完成状态。

## 11. 完成后的架构

```text
CLI new/resume/replay/list/approvals
          ↓
SessionStore + event codec + privacy policy
          ↓
AgentSessionState reducer/checkpoint
          ↓
model response normalization
          ↓
tool policy + approval audit + VerificationToolState
          ↓
workspace guard + interrupted-call reconciliation
          ↓
append-only JSONL / content-addressed artifacts
          ↓
offline replay and audit timeline
```

M4 完成后，代理才具备可靠的跨进程任务连续性和可审计基础。M5 可以在不改变 session 事实模型的前提下，为命令执行加入更严格的 allowlist、敏感路径保护、进程隔离和统一权限策略。
