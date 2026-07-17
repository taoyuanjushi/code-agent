# M5 阶段详细实现指南：沙箱与权限增强

## 1. 阶段目标

M4 已经实现会话持久化、审批审计、中断恢复和只读 replay，但进程工具仍存在明显边界：`run_command` 接收 shell 字符串并通过 `shell=True` 执行，进程继承环境的范围尚未集中控制，敏感路径保护与普通 `.gitignore` 规则没有分离，也没有可验证的隔离后端。

M5 的目标不是承诺“绝对安全”，而是把命令从“字符串 + 用户确认”升级为“结构化命令 + 集中策略 + 受控执行 + 可选 Docker 隔离”，并保证任何无人值守命令都不会静默回退到宿主机执行。

完成后应形成以下链路：

```text
工具 argv / 已发现验证命令
          ↓
路径与敏感信息策略
          ↓
命令规范化、分类和硬拒绝
          ↓
交互审批或 sandbox 强制门禁
          ↓
HostProcessRunner / DockerSandbox
          ↓
受限环境、资源预算和结构化结果
          ↓
SessionStore 审计、恢复和 replay
```

M5 不实现多租户强隔离、远程执行平台、Kubernetes、完整 seccomp 策略生成器或 Windows AppContainer。Docker 是可选后端；没有 Docker 时仍可交互运行有限的宿主机命令，但不能启用 full-auto。

## 2. 学习目标与先修知识

建议先掌握：

- `subprocess.Popen()`、argv、`shell=False`、进程组和超时终止；
- `os.lstat()`、符号链接、Windows reparse point、TOCTOU 和原子替换；
- allowlist、denylist、fail closed、least privilege 和 capability；
- Docker bind mount、只读 rootfs、network namespace、Linux capabilities 和 image digest；
- 当前调用链：`tools.py → verification.py/subprocess`，以及 M4 的审批、事件、reducer 和恢复模型；
- “用户批准”“命令策略允许”和“命令运行在沙箱中”是三个不同事实。

开始前固定 M4 基线：

```powershell
.\.venv\Scripts\python.exe -m pytest -q --basetemp=.coding-agent\pytest-m5-baseline
```

## 3. 设计原则

### 3.1 策略判断必须早于审批和 subprocess

命令先规范化并得到稳定的策略决定，硬拒绝命令不能通过人工批准、自动批准或 resume 绕过。被拒绝时必须保证 subprocess 调用次数为 0。

### 3.2 生产代码禁止拼接 shell 字符串

模型工具、验证命令、Git 内部命令和 Docker CLI 都使用 argv。M5 完成后，生产代码中不应存在 `shell=True`。shell、批处理、PowerShell、`python -c` 和 `node -e` 等“解释任意文本”的入口应被拒绝或强制进入 sandbox。

### 3.3 `.gitignore` 不是敏感路径策略

忽略规则解决上下文噪声，敏感路径策略解决秘密暴露。即使 `.env` 没有被 Git 忽略，也不能被读取、搜索、快照复制或作为 artifact 展开；`.env.example` 等安全模板要通过显式例外处理。

### 3.4 宿主机执行不是安全沙箱

审批只能表达用户意图，不能限制进程读取文件、访问网络或创建子进程。宿主机执行仅支持受控、可解释的命令；任意解释器、安装、网络和复杂构建命令要求 Docker。full-auto 必须在 sandbox capability 检查失败时直接退出。

### 3.5 沙箱运行副本，不直接修改真实 workspace

Docker 使用过滤后的临时 workspace snapshot，排除忽略项、session 数据和敏感路径。容器内产生的修改在结束后丢弃；真实源码修改仍只能通过宿主机上的 `apply_patch` 完成并进入审批审计。

### 3.6 安全决定必须可审计但不能泄密

记录策略版本、规则 ID、归一化 argv、后端、image digest、资源限制和结果摘要；不记录完整环境变量、秘密值或敏感文件正文。旧 session 的安全配置不能在 resume 时被新 CLI 参数静默覆盖。

## 4. 建议目录与领域模型

新增独立子包，避免继续扩大 `tools.py`：

```text
src/coding_agent/security/
  __init__.py
  models.py          命令、策略决定、资源限制和 sandbox 类型
  path_policy.py     敏感路径、realpath、symlink/reparse point 规则
  command_policy.py  argv 规范化、allowlist/denylist 和风险分类
  process_runner.py  shell=False、环境白名单、超时和输出预算
  snapshot.py        过滤后的临时 workspace 副本
  sandbox.py         后端协议、capability 和统一执行入口
  docker_backend.py  Docker 探测、argv 构造、运行和清理
```

建议核心模型：

```python
CommandDisposition = Literal[
    "allow_host",
    "approval_required",
    "sandbox_required",
    "deny",
]

@dataclass(frozen=True)
class ExecutionLimits:
    timeout_ms: int = 30_000
    max_output_bytes: int = 32 * 1024
    max_output_lines: int = 200
    memory_mb: int = 1024
    pids_limit: int = 256
    cpus: float = 2.0

@dataclass(frozen=True)
class CommandSpec:
    argv: tuple[str, ...]
    cwd: str
    source: Literal["internal", "verification", "tool"]
    purpose: str
    limits: ExecutionLimits

@dataclass(frozen=True)
class CommandPolicyDecision:
    disposition: CommandDisposition
    rule_id: str
    reasons: tuple[str, ...]
    normalized_executable: str
    requires_approval: bool
    requires_sandbox: bool

@dataclass(frozen=True)
class SandboxCapability:
    backend: Literal["host", "docker"]
    available: bool
    reason: str | None
    image_reference: str | None
    image_digest: str | None
```

所有字段都要在构造期校验并显式序列化。不要直接持久化 `CompletedProcess`、Docker SDK 对象或任意异常对象。

## 5. 详细实现步骤

### 5.1 第一步：先固定 M5 验收指标

先新增 `tests/test_m5_acceptance.py`，不要先修改生产代码。固定以下合同：

1. 所有生产 subprocess 都使用 argv 和 `shell=False`；测试扫描不得发现 `shell=True`。
2. hard-deny 命令、敏感路径和 workspace 逃逸在调用 subprocess 或打开文件前失败。
3. `run_command` 默认输出最多 32 KiB、200 行，默认超时 30 秒，最大超时 300 秒。
4. 子进程环境只包含白名单键；测试注入的 `OPENAI_API_KEY`、`*_TOKEN`、`*_SECRET`、`*_PASSWORD` 均不可见。
5. `.env`、`.env.*`、私钥、认证配置、`.coding-agent/` 不进入列表、搜索、读取或 sandbox snapshot；`.env.example` 可读取。
6. 指向 workspace 外的 symlink/junction 不可读取；任何带 symlink/reparse point 路径的写操作均被拒绝。
7. full-auto 在 Docker 不可用、image 不存在、digest 无法确定或 network 不是 `none` 时启动失败，且发生在模型调用之前。
8. Docker argv 固定包含 `--network none`、`--read-only`、`--cap-drop ALL`、`no-new-privileges`、PID/内存/CPU 限制和临时 `/tmp`。
9. sandbox snapshot 最多 10,000 个文件、512 MiB；超限时失败，不生成部分可执行快照。
10. 策略决定、审批、sandbox capability、image digest、限制和执行结果可从 session replay 审计。
11. M1-M4 的 406 项测试继续通过。

Docker 真实运行测试使用 `@pytest.mark.docker`，默认测试矩阵只验证纯策略、argv 构造和模拟后端，不要求开发机安装 Docker。

> 实现状态：第一步已完成。`tests/test_m5_acceptance.py` 不导入尚未实现的 `coding_agent.security` 生产模块，而是以 12 个合同测试固定 argv/`shell=False` 边界、拒绝发生在副作用之前、输出与超时预算、环境白名单、敏感路径、链接路径、full-auto 前置门禁、Docker 隔离参数、snapshot 预算、session 审计、M1-M4 的 406 项基线以及 Docker 测试标记策略。该状态只表示 M5 验收目标已经冻结，不表示沙箱与权限增强功能已经实现。

### 5.2 第二步：定义安全领域模型和策略版本

新增 `security/models.py`，先完成不可变模型和严格校验，不执行进程。

实现顺序：

1. 定义 `ExecutionLimits`、`CommandSpec`、`CommandPolicyDecision`、`SensitivePathDecision`、`SandboxCapability`、`SandboxExecutionPlan` 和 `SecureExecutionResult`。
2. argv 必须是非空字符串元组，拒绝 NUL、空参数中的非法编码和超出长度预算的参数。
3. cwd 使用 workspace-relative POSIX 路径持久化，运行前再转换成本机绝对路径。
4. 为策略增加常量版本，例如 `SECURITY_POLICY_VERSION = 1`。
5. 结果区分 `passed`、`failed`、`timed_out`、`denied`、`sandbox_unavailable` 和 `internal_error`。
6. 对所有模型提供显式 `to_dict()`/`from_dict()`，未知字段和未知版本必须拒绝。

测试文件建议为 `tests/test_security_models.py`。重点覆盖非法 disposition、冲突布尔值、负数资源限制、超长 argv、未知后端和 round-trip。

> 实现状态：第二步已完成。新增 `src/coding_agent/security/models.py` 和 `security/__init__.py`，定义 `SECURITY_SCHEMA_VERSION = 1`、`SECURITY_POLICY_VERSION = 1` 以及 7 个不可变安全领域模型。模型在构造期严格校验 argv 数量、单参数/总字节预算、NUL 与 UTF-8、workspace-relative POSIX cwd、disposition 布尔一致性、资源限制、Docker digest、后端/网络组合和结果状态；所有模型均提供拒绝未知字段与未知版本的显式 `to_dict()`/`from_dict()`。`tests/test_security_models.py` 以 54 项测试覆盖默认值、非法输入、状态冲突、JSON round-trip 和嵌套版本拒绝。本步骤不执行 subprocess，也尚未接入命令或路径策略。

### 5.3 第三步：集中敏感路径策略

新增 `security/path_policy.py`，不要把安全规则混入 `IgnorePolicy`。推荐接口：

```python
@dataclass(frozen=True)
class SensitivePathPolicy:
    root: Path
    denied_names: frozenset[str]
    denied_suffixes: frozenset[str]
    allowed_exceptions: frozenset[str]

    def evaluate(self, path: str | Path, *, operation: str) -> SensitivePathDecision: ...


def load_sensitive_path_policy(workspace: str | Path) -> SensitivePathPolicy: ...
```

默认拒绝至少覆盖：

- `.env` 和 `.env.*`，但允许 `.env.example`、`.env.sample`；
- `.npmrc`、`.pypirc`、`.netrc`、`credentials`、`credentials.json`；
- `.ssh/`、`.aws/`、`.config/gcloud/`；
- `id_rsa`、`id_ed25519`、`*.pem`、`*.key`、`*.p12`、`*.pfx`；
- `.coding-agent/`，防止模型工具或 sandbox 读取自身 session。

将该策略接入 `read_file`、`read_many_files`、`list_files`、`search_text`、初始上下文、artifact 展开和 snapshot。显式请求被拒绝时返回稳定 reason code，例如 `sensitive_path_denied`；清单和搜索不应泄露被过滤文件的正文。

测试覆盖大小写差异、Windows 分隔符、嵌套目录、允许例外、否定 `.gitignore` 不能覆盖敏感规则，以及敏感 symlink 的最终目标判断。

> 实现状态：第三步已完成。新增 `src/coding_agent/security/path_policy.py`，以独立于 `IgnorePolicy` 的不可变 `SensitivePathPolicy` 集中处理大小写无关的敏感文件名、后缀、目录树和 `.env` 安全例外，并统一返回 `SensitivePathDecision` 与稳定 reason code `sensitive_path_denied`。策略已经接入 `read_file`、`read_many_files`、`list_files`、`search_text`、初始上下文、`AGENTS.md` 指令发现和 verbose replay artifact 展开；显式读取、列表和搜索会结构化拒绝，枚举、rg 结果、Python fallback 与上下文采样会静默过滤。带 `source_path` 的 artifact 在展开前重新执行策略，接口同时保留 `snapshot` operation 供后续 sandbox snapshot 复用。`tests/test_sensitive_paths.py` 覆盖默认规则、大小写和 Windows 分隔符、`.gitignore` 否定、列表/搜索/读取/上下文、rg 过滤、敏感指令、读取前拒绝、内部敏感 symlink 目标及 artifact 展开；symlink 测试在平台不支持创建链接时条件跳过。

### 5.4 第四步：强化 realpath、symlink 和 Windows 路径边界

扩展或替换 `path_safety.py`，把“读取”“创建”“覆盖”“执行 cwd”和“快照复制”区分开。

推荐接口：

```python
PathOperation = Literal["read", "write", "execute", "snapshot"]


def resolve_workspace_path(
    workspace: str | Path,
    requested_path: str | Path,
    *,
    operation: PathOperation,
    allow_missing: bool = False,
) -> Path: ...
```

规则：

1. 拒绝 NUL、UNC、设备路径、驱动器相对路径和 Windows alternate data stream。
2. 先做词法 containment，再对 workspace 和候选路径做 realpath containment。
3. 读取可跟随 symlink，但最终目标必须仍在 workspace 内且不敏感。
4. 写入、补丁和 snapshot 不跟随任何 symlink；现有路径组件若是 symlink 或 reparse point，直接拒绝。
5. 非现存目标检查最近的现存父目录，并在真正打开、替换或复制前再次校验。
6. `apply_patch` 的 plan 阶段和 commit 阶段都要校验，避免审批后路径被替换。
7. `list_files` 和搜索不递归进入 symlink 目录。

Windows 额外检查 `FILE_ATTRIBUTE_REPARSE_POINT`；POSIX 使用 `lstat()`。测试至少覆盖内部 symlink、外部 symlink、断链、symlink 父目录、junction/reparse point 模拟、`..`、绝对路径和补丁审批后的路径替换。

> 实现状态：第四步已完成。`src/coding_agent/path_safety.py` 现提供按 `PathOperation` 统一校验的 `resolve_workspace_path()`：它拒绝 NUL、UNC、设备路径、驱动器相对路径和 ADS，先验证词法边界，再验证 realpath 边界；读取只允许最终目标仍在 workspace 内，写入、执行、列表、搜索和 snapshot 则拒绝任一 symlink 或 Windows reparse point。补丁规划与提交、文件读取、搜索与上下文枚举、指令和 `.gitignore` 发现、验证命令元数据、session 存储、resume/recovery 以及工具 cwd 均在副作用前复检路径；目录遍历显式不跟随链接。`tests/test_path_safety.py`、`test_patch.py`、`test_tools.py`、`test_search.py`、`test_context.py`、`test_session_store.py` 与验证发现测试覆盖内部/外部/断链链接、父目录替换、reparse 模拟、Windows 语法和外部元数据链接；无法创建 symlink 的平台会条件跳过对应案例。

### 5.5 第五步：把命令工具改为结构化 argv

先修改工具 schema，再迁移执行实现。`run_command` 不再接受 shell 字符串：

```json
{
  "argv": ["python", "-m", "pytest", "tests/test_patch.py", "-q"],
  "cwd": ".",
  "timeout_ms": 30000
}
```

要求：

1. schema 将 `argv` 设为必填数组，每项为非空字符串；`cwd` 为可选 workspace-relative 路径。
2. 不提供把旧 `command` 字符串自动交给 shell 的兼容分支；旧调用返回明确迁移错误。
3. `git_status`、`git_diff` 和 Docker 探测也迁移到共享 argv runner。
4. TypeScript 在 Windows 上可能解析到 `.cmd`/`.bat` wrapper；它们不能获得无人值守宿主机权限，参数必须经过单独规则并优先进入 Docker。
5. approval request 展示 shell-escaped 仅用于 UI，实际执行始终使用原始 argv。

测试要证明参数中的空格、引号、`&&`、`|`、`>` 和 `$()` 都作为普通 argv 内容传递，不产生第二条命令。

> 实现状态：第五步已完成。`run_command` schema 现在只接受非空 `argv` 数组、可选 workspace-relative `cwd` 和受限 `timeout_ms`；旧 `command` 字符串会返回明确迁移错误，绝不会自动 split 或交给 shell。`git_status` 与 `git_diff` 已迁移到共享 argv 执行器，执行前及启动 subprocess 前都会用 `operation="execute"` 重新校验 cwd，`subprocess.run` 显式使用 `shell=False`。审批界面仅用 `list2cmdline()` 渲染 argv，审批记录、工具结果和 session 执行审计保留结构化 argv。`tests/test_tools.py` 覆盖 schema、legacy 拒绝、cwd 边界、特殊参数原样传递和内部 Git；会话/审批/恢复/集成测试同步验证该迁移。

### 5.6 第六步：实现命令 allowlist/denylist 与风险分类

新增 `security/command_policy.py`。不要只匹配可执行文件名；规则至少包含“解析后的 executable + subcommand + 参数约束 + 来源”。

建议决定顺序：

1. **hard deny**：提权、磁盘/系统管理、宿主机容器控制、破坏性 Git、设备路径、未知 shell wrapper。
2. **sandbox required**：shell、PowerShell、批处理、`python -c`、`node -e`、包安装、网络客户端、任意脚本解释器和未发现的构建命令。
3. **verification allowlist**：仅允许本轮 `discover_verification_commands` 返回的 command ID 和完全匹配 argv。
4. **internal allowlist**：固定的 `git status --short`、`git diff --stat`、`git diff` 等只读命令。
5. **interactive host**：少量可解释命令可在宿主机经审批运行。
6. 其他命令默认 `sandbox_required`，而不是默认放行。

硬拒绝示例包括 `sudo`、`su`、`doas`、`runas`、`git reset --hard`、`git clean`、`git checkout`、`docker`、`podman` 和直接调用系统 shell。规则返回稳定的 `rule_id`，例如 `deny.destructive_git`、`sandbox.inline_interpreter`、`allow.discovered_verification`。

审批处理必须在策略之后：`deny` 永远不能批准；`sandbox_required` 在无 sandbox 时不能通过用户输入降级为 host。

**实现状态（已完成）**：已新增 `security/command_policy.py`，按 hard deny、sandbox required、验证 allowlist、内部 allowlist、交互宿主机和默认 sandbox 的顺序返回版本化决定。`run_command`、`run_verification`、`git_status` 与 `git_diff` 均在审批和 subprocess 之前执行策略；阻断结果包含稳定的 `status`、`rule_id`、`disposition` 和完整 `policy` 字段。未知 argv、shell、内联解释器、网络/安装命令及未发现构建命令在当前无 sandbox 后端时 fail closed，不会请求审批或回退到宿主机。对应单元与集成测试位于 `tests/test_command_policy.py`、`tests/test_tools.py` 和 `tests/test_verification_tools.py`。

### 5.7 第七步：实现受控宿主机进程执行器

新增 `security/process_runner.py`，并让 `verification.py` 与 `tools.py` 共用。推荐使用 `Popen`，不要继续使用无限内存的 `capture_output=True`。

执行要求：

1. `shell=False`、`stdin=DEVNULL`、规范化 cwd。
2. 使用环境白名单构造新环境，不复制完整 `os.environ`。基础键可包含 `PATH`、`SYSTEMROOT`、临时目录和 locale；秘密命名模式一律排除。
3. stdout/stderr 写入受控临时文件或增量读取，在 32 KiB/200 行预算内压缩，完整大输出按 M4 privacy policy artifact 化。
4. POSIX 使用新进程组并在超时时终止整个组；Windows 使用新进程组和受控的进程树终止适配器。
5. 记录实际 executable、argv、cwd、允许的环境键名、超时、耗时、退出码和截断信息，不记录环境值。
6. runner 只执行已经获得 `allow_host` 或完成审批的计划；自身不重新猜测策略。

把 M3 `run_verification_command()` 的 subprocess 代码迁移到共享 runner，并保持现有 `VerificationResult` 结构和输出压缩语义不变。

**实现状态（已完成）**：已新增 `security/process_runner.py`，通过 `Popen`、`shell=False`、`DEVNULL` 和受保护的 workspace-relative cwd 执行已授权命令；子进程环境从白名单重建并排除 secret 命名模式，stdout/stderr 采用增量读取并受字节/行数预算约束。POSIX 使用独立进程组，Windows 使用新进程组和进程树终止适配器；超时会终止整棵进程树。执行结果记录实际 executable、argv、cwd、允许的环境键名、耗时、退出码和截断信息，但不记录环境值。`verification.py` 与 `tools.py` 已共用该 runner，且 runner 会在创建进程前拒绝未获 `allow_host` 或未完成审批的计划。对应测试位于 `tests/test_process_runner.py`、`tests/test_verification_runner.py` 和 `tests/test_tools.py`。

### 5.8 第八步：生成过滤后的 sandbox workspace snapshot

新增 `security/snapshot.py`。容器不能直接 bind mount 真实 workspace，否则命令仍可读取 `.env`、session 日志或修改源码。

流程：

1. 在 `.coding-agent/sandboxes/<session-id>/<call-id>/workspace/` 创建临时目录。
2. 使用 `IgnorePolicy`、`SensitivePathPolicy` 和强化后的 path guard 枚举文件。
3. 不复制 `.git`、`.coding-agent`、虚拟环境、依赖缓存、敏感路径、二进制大文件和任何 symlink。
4. 先生成 manifest 并检查 10,000 文件/512 MiB 预算，再开始复制；失败时不留下可执行的部分快照。
5. manifest 记录相对路径、大小和 SHA-256，session 只保存 manifest hash、文件数、字节数和排除原因计数。
6. 容器执行结束后删除快照；清理失败写审计事件，但不能把清理失败伪装成命令成功。
7. 容器内修改不回写真实 workspace；代码变更仍由 `apply_patch` 完成。

测试覆盖敏感文件排除、忽略规则、内部/外部 symlink、预算超限、复制期间文件变化、部分复制失败和确定性 manifest。

#### 实现状态（已完成）

- 新增 `src/coding_agent/security/snapshot.py`，统一使用 `IgnorePolicy`、`SensitivePathPolicy` 和 `operation="snapshot"` 路径守卫。
- 在创建 staging 前完成确定性枚举、10,000 文件/512 MiB 预算检查和逐文件 SHA-256；大型二进制、敏感路径、忽略项、symlink/reparse point 与非普通文件不会进入副本。
- 使用同 session 目录内的 staging 构建 `workspace/` 和规范化 `manifest.json`，复制期间复检文件身份、摘要与最终清单，成功后再原子发布。
- 提供幂等清理和安全审计摘要；session 只需持久化 manifest hash、文件数、总字节和排除计数。
- `tests/test_sandbox_snapshot.py` 覆盖过滤、嵌套 `.gitignore`、预算失败、源文件变化、部分复制失败、确定性、隔离性及安全清理。
### 5.9 第九步：实现可选 Docker sandbox 后端

定义 `SandboxBackend` 协议，再实现 `DockerSandboxBackend`。优先调用本机 Docker CLI，不新增 Docker Python SDK 运行时依赖。

capability 探测：

1. 使用 argv 执行 `docker version` 和 `docker image inspect`。
2. 不自动 pull image；image 不存在时返回 `sandbox_unavailable`。
3. 解析并固定本地 image ID/digest；tag 指向变化时 resume 必须拒绝或重新批准。
4. 初版只承诺 Linux container；Windows container 模式返回明确不支持原因。

`docker run` 至少包含：

```text
--rm
--name coding-agent-<session>-<call>
--network none
--read-only
--cap-drop ALL
--security-opt no-new-privileges=true
--pids-limit 256
--memory 1024m
--cpus 2
--user 65532:65532
--tmpfs /tmp:rw,nosuid,nodev,noexec,size=64m
--mount type=bind,src=<snapshot>,dst=/workspace
--workdir /workspace/<cwd>
```

同时：

- 使用非 root 用户；
- 不挂载 Docker socket、SSH agent、用户 home、真实 `.git` 或 session 目录；
- 只传环境白名单；
- 容器名由 session/call ID 确定，超时后用 argv 执行 `docker rm -f`；
- 容器退出后验证快照仍位于预期目录，再清理。

模拟测试断言完整 Docker argv；真实 smoke test 放入 `tests/test_docker_sandbox_smoke.py` 并标记 `docker`。

#### 实现状态（已完成）

- 新增 `security/sandbox.py`，定义 `SandboxBackend` 协议、执行授权错误和包含容器/快照清理事实的结构化结果。
- 新增 `security/docker_backend.py`，通过受控 host runner 仅执行后端生成的 Docker argv；探测本地 Linux daemon 和 image ID，不自动 pull，并在每次执行前拒绝 image digest 漂移。
- `docker run` 固定使用无网络、只读 rootfs、去 capabilities、`no-new-privileges`、非 root 用户、资源限制、`--pull never` 和过滤后的 workspace snapshot；只继承 `LANG`/`LC_ALL`，其余容器环境使用安全固定值。
- 超时后使用 `docker rm -f <container>` 对账；无论命令结果如何都安全清理 snapshot，清理失败单独进入 outcome，不覆盖命令状态。
- `tests/test_docker_backend.py` 纯模拟覆盖 capability、完整 argv、digest 漂移、审批、超时和清理审计；`tests/test_docker_sandbox_smoke.py` 通过 `CODING_AGENT_RUN_DOCKER_TESTS=1` 显式启用真实 Docker smoke test。
### 5.10 第十步：增加 CLI 安全配置和 full-auto 门禁

建议新增：

```text
--sandbox {none,auto,docker}
--sandbox-image IMAGE
--full-auto
```

规则：

1. 默认 `--sandbox auto`：普通交互命令可使用严格 host policy；需要 sandbox 时探测 Docker，不可用则拒绝该命令。
2. `--full-auto` 隐含 `--write`、自动批准编辑和命令，但必须成功选择 Docker、固定本地 image digest，并保持 `network=none`。
3. `--auto-approve-commands` 也不得在 host backend 上启用；保留该参数时应要求 sandbox capability。
4. `--auto-approve-edits` 仍只能批准通过 path guard 和 unified diff 校验的 `apply_patch`。
5. 参数冲突、Docker 不可用和 image 未固定必须在创建模型客户端前失败。
6. 配置写入 `AgentConfig`、session safe config、codec 和 replay；resume 不接受新的 sandbox/image/full-auto 覆盖。

不要在 Docker 失败时打印警告后自动回退到 host。fail closed 是本步骤的核心验收条件。

**实现状态（已完成）**：CLI 已新增 `--sandbox {none,auto,docker}`、`--sandbox-image` 和 `--full-auto`；`full-auto` 隐含写权限及编辑/命令自动批准。显式 Docker、full-auto 和自动批准命令会在 agent/model 启动前探测本地 Linux Docker 与 image digest，失败直接退出且不回退 host；普通 `auto` 模式延迟探测。sandbox mode、image、固定 digest 和 full-auto 会进入 `AgentConfig`、session 安全配置、codec/replay，并在 resume 时禁止 CLI 覆盖。Docker 工具路由已在第十一步接入，固定 Docker 的命令与验证始终不会降级到宿主机。

### 5.11 第十一步：接入工具 schema、提示词和结构化返回

更新 `tools.py`、`tool_policy.py`、`agent.py` 和提示词：

- `run_command` 使用 argv/cwd；
- `run_verification` 继续只接受已发现 command ID；
- 提示模型优先使用专用读取、搜索、补丁和验证工具，不用命令绕过敏感路径策略；
- 明确禁止请求安装依赖、开启网络、启动 shell 或读取秘密；
- 被策略拒绝后，模型应选择安全替代方案或向用户解释限制，不能改写命令进行规避。

结构化返回建议包含：

```json
{
  "type": "secure_command_result",
  "argv": ["python", "-m", "pytest", "-q"],
  "cwd": ".",
  "backend": "docker",
  "sandboxed": true,
  "policy_version": 1,
  "rule_id": "allow.discovered_verification",
  "image_digest": "sha256:...",
  "exit_code": 0,
  "timed_out": false,
  "duration_ms": 842,
  "output_truncated": false
}
```

审批 UI 需要展示后端、cwd、argv、sandbox 限制和规则原因，而不是只显示一段命令字符串。

**实现状态（已完成）**：`run_command` 仅接受结构化 `argv`/workspace-relative `cwd`，`run_verification` 仅接受 discovery command ID；两者会按策略选择受控 host runner 或共用 Docker snapshot 后端，并统一返回 `secure_command_result`。结构化结果及 session 执行审计包含 backend、sandbox、image digest、policy rule、超时、截断和清理信息。审批展示加入后端、网络隔离、镜像、规则与原因；系统提示词明确禁止 shell、wrapper、inline interpreter、依赖安装、网络、秘密读取及策略规避。显式 Docker 和 sandbox-required 路径 fail closed，测试保证不会回落宿主机。

### 5.12 第十二步：接入 SessionStore、reducer、resume 和 replay

M5 必须沿用 M4 事实日志，不建立第二套审计文件。建议增加事件：

```text
security.policy_evaluated
sandbox.capability_checked
sandbox.snapshot_created
sandbox.started
sandbox.finished
sandbox.cleanup_failed
```

持久化内容：

- policy version、rule ID、reason codes；
- 规范化 argv/cwd 和参数 hash；
- backend、image reference/digest、network mode 和资源限制；
- snapshot manifest hash、文件数和总字节数；
- approval source/outcome、退出码、超时和输出 artifact。

恢复规则：

1. policy version、backend 或 image digest 漂移时阻止 resume。
2. 已完成 call ID 复用原结果，不重新创建容器。
3. 中断容器按确定性名称查询和清理；结果未知时不得伪造成功。
4. host process 继续沿用 M4 的重新审批规则。
5. full-auto 的隔离进程只有在确认无网络、只操作临时 snapshot 且旧容器已清理后，才允许自动重试，并记录 recovery 事件。
6. replay 只展示历史，不执行 Docker 探测或清理。

更新 session codec 时保持旧 schema 可读；不要让 M5 事件破坏 M4 session replay。

**实现状态（已完成）**：沿用 M4 `SessionStore` JSONL/hash chain、reducer、artifact 和审批事实日志，新增 policy evaluated、Docker capability、snapshot、sandbox start/finish 与 cleanup failure 事件。新 session 固定 security policy version；resume 会重新探测已使用的 Docker backend，并在 policy version、backend 可用性或 image digest 漂移时拒绝恢复。完成的 call ID 继续复用原结果；host 中断继续要求 `resume_recovery` 审批。中断 Docker 使用持久化的确定性容器名执行 inspect/remove；只有 full-auto、`network=none`、临时 snapshot、digest 一致且旧容器已确认清理时才自动重试，否则重新审批或阻断。replay 只读取并展示安全事件，不探测或清理 Docker；没有 M5 字段的旧 session 仍可读取和恢复。

### 5.13 第十三步：补齐安全与跨平台测试矩阵

建议测试文件：

```text
tests/test_security_models.py
tests/test_sensitive_paths.py
tests/test_path_safety.py
tests/test_command_policy.py
tests/test_process_runner.py
tests/test_sandbox_snapshot.py
tests/test_docker_backend.py
tests/test_cli_security.py
tests/test_m5_integration.py
tests/test_m5_acceptance.py
tests/test_docker_sandbox_smoke.py
```

矩阵至少覆盖：

| 场景 | 预期 |
| --- | --- |
| argv 含 `&&`、`>`、空格和引号 | 作为普通参数，不执行第二条命令 |
| hard-deny 命令 | subprocess 调用数为 0 |
| 未知命令 | 要求 sandbox，不默认 host 放行 |
| 环境中存在 API key/token | 子进程和事件日志均看不到值 |
| 读取 `.env` 或私钥 | 稳定拒绝，正文不进入输出 |
| 外部 symlink/junction | 读取、写入和 snapshot 均拒绝 |
| 审批后替换父目录为 symlink | 应用前复检失败 |
| 命令超时并创建子进程 | 整个进程树终止 |
| 输出远超预算 | 结构化截断，内存不随输出无限增长 |
| Docker 不可用 | 交互命令明确失败；full-auto 启动失败 |
| Docker image tag 漂移 | resume 拒绝 |
| snapshot 含敏感/忽略文件 | 文件不被复制，manifest 不包含正文 |
| Docker argv | 网络、capability、rootfs 和资源限制齐全 |
| sandbox 中修改源码 | 真实 workspace 不变化 |
| replay | 不调用 subprocess、Docker 或输入函数 |
| M1-M4 回归 | 全量通过 |

Windows 和 POSIX 的路径、可执行文件后缀、进程树终止逻辑分开测试。Docker smoke 可以跳过，但 Docker argv 与 fail-closed 单元测试不能跳过。

**实现状态（已完成）**：安全矩阵已绑定实际生产常量，并补齐 `tests/test_m5_integration.py` 的工具、SessionStore、reducer 与只读 replay 端到端链路。Windows/POSIX 进程组启动和进程树终止、Windows 可执行文件后缀、敏感路径与链接/TOCTOU、输出和超时预算、snapshot 过滤、Docker hardened argv、`auto` 模式 Docker 不可用时禁止 host fallback、sandbox 写入不污染真实 workspace、image digest 漂移和离线 replay 均有独立测试。测试同时修复了安全策略预检拒绝无法被 reducer 持久化的问题，并保留对正常命令审批不可绕过的反例。真实 Docker smoke 继续由 `docker` marker 和环境变量控制，Docker 单元合同默认必跑。

### 5.14 第十四步：更新文档、版本和最终验收

功能完成后同步：

- `README.md`：说明 host 与 Docker 边界、full-auto 前置条件、敏感路径默认值；
- `docs/implementation-plan.md`：仅在全部验收通过后把 M5 标为已完成；
- `.env.example`：不新增秘密值，仅增加安全的 sandbox 配置示例；
- 包版本：M5 完成后建议从 `0.3.x` 升到 `0.4.0`；
- wheel：确认新 `security/` 包、CLI schema 和依赖元数据被包含。

建议验收命令：

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_m5_acceptance.py tests\test_command_policy.py tests\test_sensitive_paths.py -q --basetemp=.coding-agent\pytest-m5-core
.\.venv\Scripts\python.exe -m pytest tests\test_m5_integration.py tests\test_cli_security.py -q --basetemp=.coding-agent\pytest-m5-integration
.\.venv\Scripts\python.exe -m pytest -m "not docker" -q --basetemp=.coding-agent\pytest-m5-default
.\.venv\Scripts\python.exe -m pytest -q --basetemp=.coding-agent\pytest-m5-full
.\.venv\Scripts\python.exe -m compileall -q src tests
.\.venv\Scripts\python.exe -m pip wheel . -w dist
git diff --check
```

有 Docker 的受控环境再执行：

```powershell
.\.venv\Scripts\python.exe -m pytest -m docker -q --basetemp=.coding-agent\pytest-m5-docker
```

**实现状态（已完成）**：README 已说明 host/Docker 边界、full-auto 前置条件和默认敏感路径，`.env.example` 保持无秘密值的 `auto`/本地 image 示例；总实施计划已把 M5 标为完成，包版本已从 `0.3.0` 升至 `0.4.0`。最终验收结果为核心安全合同 81 项、M5 集成/CLI 11 项、非 Docker 默认矩阵 640 项（1 skipped、1 deselected）和全量测试 640 项（2 skipped）通过，`compileall` 与 `git diff --check` 通过。当前包索引无法提供隔离构建依赖，因此按 README 的离线命令使用本地 setuptools 82.0.1 构建 `dist/coding_agent-0.4.0-py3-none-any.whl`（152571 字节、44 个文件，SHA-256 `f6517d4bcc6fb82e25dbf6be951af2877b099019a0e26b284a87bc2d9183589d`）；wheel 的版本、Python 要求、依赖和 console entry 元数据正确，包含完整 `security/`，不包含 `tests/`、`.env` 或 `.coding-agent/`，并通过隔离目录安装、`coding_agent.__version__`、security/SessionStore 导入、CLI parser 和 `coding-agent --help` 返回码 0 的 smoke 验证。真实 Docker smoke 保持 opt-in，本机未执行。

## 6. 推荐命令策略示例

| 命令 | 来源 | 建议决定 | 原因 |
| --- | --- | --- | --- |
| `git status --short` | internal | `allow_host` | 固定只读 argv |
| `python -m pytest -q` | discovered verification | `approval_required` 或 sandbox | 已发现验证命令 |
| `npm test -- --runInBand` | discovered verification | Windows 优先 sandbox | 可能经过 `.cmd` wrapper |
| `python -c "..."` | tool | `sandbox_required` | 可执行任意代码 |
| `pip install ...` | tool | `sandbox_required` | 修改环境且可能联网 |
| `curl ...` | tool | `sandbox_required`，M5 默认仍拒绝网络 | 外部网络副作用 |
| `git reset --hard` | 任意 | `deny` | 破坏 workspace |
| `docker run ...` | model tool | `deny` | 不允许嵌套控制宿主 Docker |
| `powershell -Command ...` | tool | `sandbox_required` | shell 文本执行 |

allowlist 是结构化规则，不是“命令字符串以 `python` 开头”。例如 `python -m pytest` 与 `python -c` 必须得到不同决定。

## 7. 常见误区

### 7.1 把用户批准当作沙箱

批准只能确认意图，无法阻止命令读取 home、访问网络或启动子进程。高风险命令仍需要隔离，hard deny 仍不能执行。

### 7.2 用正则继续维护 shell 字符串

字符串可以利用引号、转义、换行、变量展开和子 shell 绕过规则。正确做法是工具层只接受 argv，并禁止通用 shell 入口。

### 7.3 只 allowlist 可执行文件名

允许 `python` 等于允许执行任意 Python；允许 `git` 也可能包含 `reset --hard`。规则必须包含 subcommand、参数和命令来源。

### 7.4 直接把真实 workspace 挂进 Docker

这会让容器读取 `.env`、`.coding-agent` 和其他秘密，也能修改真实文件。M5 应运行过滤后的临时 snapshot，修改结果不回写。

### 7.5 把完整 `os.environ` 传给子进程

即使 session 日志会脱敏，进程仍可能上传或打印 API key。执行器必须从空字典开始构造环境白名单。

### 7.6 只在任务开始时检查 symlink

审批和真正写入之间路径可能变化。补丁、snapshot 和进程 cwd 必须在副作用前再次校验。

### 7.7 Docker 不可用时自动回退 host

这会破坏 full-auto 的安全承诺。正确行为是 fail closed，明确告诉用户缺少哪项 capability。

### 7.8 自动 pull “方便使用”

自动 pull 会产生网络和供应链副作用。M5 只使用本地已存在并可解析 digest 的 image。

## 8. 调试与排错

### 合法命令被判定为 sandbox_required

输出 `rule_id`、归一化 executable、subcommand 和来源。不要直接扩大 executable allowlist；先为具体 argv 形状添加最小规则和回归测试。

### Windows 上 npm 验证无法在 host 执行

检查实际解析到的是 `.exe`、`.cmd` 还是 `.bat`。批处理 wrapper 不应获得无人值守 host 权限；使用 Docker 或保留交互审批。

### snapshot 缺少依赖

这是预期边界：`.venv`、`node_modules` 和缓存默认不复制。使用包含依赖的本地预构建 image，不要自动联网安装，也不要把宿主依赖目录整体挂载进去。

### Docker 可用但 image 不存在

返回 `sandbox_unavailable` 和所需 image reference。由用户在代理之外准备 image；agent 不自动 pull。

### 超时后仍有子进程

检查 POSIX 进程组或 Windows 进程树终止适配器是否在启动时启用，以及 cleanup 事件是否记录失败。不要只终止父 PID。

### resume 提示 image drift

tag 已指向不同 image。使用原 digest 对应 image，或创建新 session；不要在旧 session 中静默接受新运行环境。

## 9. 面试会怎么问

### 问：为什么 `shell=False` 仍然不等于安全？

答：它消除了 shell 展开和大部分注入面，但被执行程序本身仍可能删除文件、访问网络或启动其他程序，所以还需要命令策略、环境限制、路径保护、资源限制和 sandbox。

### 问：allowlist 和 denylist 应该谁优先？

答：hard deny 最先处理，用于任何模式都不能执行的动作；随后判断是否必须 sandbox；只有结构化规则明确匹配时才允许 host。未知命令默认不应 host 放行。

### 问：为什么敏感路径策略不能复用 `.gitignore`？

答：`.gitignore` 是版本控制和上下文选择规则，支持用户否定；安全策略必须 fail closed，不能因为 `!.env` 就允许模型读取秘密。

### 问：为什么 Docker 运行 snapshot 而不直接挂载仓库？

答：直接挂载会暴露被忽略文件、session 和秘密，也允许进程修改真实源码。过滤 snapshot 能控制输入集合，容器修改可丢弃，真实编辑仍走可审计补丁。

### 问：如何保证 full-auto 不回退到宿主机？

答：在模型调用前完成 backend capability 和 image digest 校验，把结果持久化到 session；每次命令再次验证 backend。任何 capability 缺失都直接拒绝，而不是修改 execution plan。

### 问：如何处理进程超时后的子进程？

答：启动时创建独立进程组或 Windows 进程树控制边界，超时时终止整个组，并记录 cleanup 结果。只杀父进程会留下后台任务和文件副作用。

### 问：为什么要记录 policy version 和 image digest？

答：同一 argv 在不同规则或 image 下可能产生不同安全结论和结果。resume 与 replay 需要知道当时使用的准确策略和运行环境，避免静默漂移。

### 问：Docker 是否提供绝对安全？

答：不能。它仍依赖宿主内核、Docker daemon、image 供应链和正确配置。M5 通过无网络、去 capability、只读 rootfs、非 root、资源限制和过滤 snapshot 降低风险，但不宣称强多租户隔离。

## 10. M5 最终验收清单

- [ ] `run_command`、验证、Git 和 Docker 调用全部使用 argv 与 `shell=False`。
- [ ] hard-deny、sandbox-required、approval-required 和 allow-host 有稳定规则与 reason code。
- [ ] 敏感路径策略独立于 `.gitignore`，并覆盖读取、搜索、列表、artifact 和 snapshot。
- [ ] 外部 symlink/junction、写入 symlink 和审批后路径替换均被拒绝。
- [ ] 子进程环境由白名单构造，已知 secret 不会继承。
- [ ] 超时会终止进程树，输出和资源有硬预算。
- [ ] verification 与通用命令共用受控 runner，M3 行为不回退。
- [x] sandbox snapshot 可确定重建、排除秘密且不回写真实 workspace。
- [x] Docker 后端默认无网络、只读 rootfs、去 capability、非 root 且资源受限。
- [x] Docker 不可用或 image 漂移时 fail closed，不自动 pull、不回退 host。
- [ ] `--auto-approve-commands` 和 `--full-auto` 只能在有效 sandbox 中启用。
- [ ] 安全策略、sandbox 计划、审批和结果可由 session replay 审计。
- [ ] resume 不重复已完成命令，不静默接受 policy/backend/image 漂移。
- [ ] 默认、跨平台、Docker 可选和 M1-M4 回归测试矩阵通过。
- [ ] README、总实施计划、版本和 wheel 验收完成。

## 11. 完成后的架构

```text
CLI safety options / persisted AgentConfig
                  ↓
SensitivePathPolicy + workspace realpath guard
                  ↓
CommandSpec normalization
                  ↓
CommandPolicyDecision
       ┌──────────┴──────────┐
       ↓                     ↓
HostProcessRunner      DockerSandboxBackend
interactive/limited    filtered snapshot/no network
       └──────────┬──────────┘
                  ↓
SecureExecutionResult + output artifact
                  ↓
approval audit / reducer / resume / replay
```

M5 完成后，项目才具备可信的无人值守执行前提。M6 可以在不放松这些边界的情况下增加流式终端 UI、计划面板、review/explain 模式和编辑器集成。
