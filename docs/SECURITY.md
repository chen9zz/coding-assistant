# RepoPilot-R 安全边界与威胁模型

## 保护对象与适用范围

RepoPilot-R 保护源仓库与工作区文件、宿主机命令执行能力、环境凭证、任务状态和审计证据。仓库内容、Issue、模型输出、MCP 工具描述与远端返回都按不可信数据处理；操作员配置的测试参数数组、工具注册表和策略 allowlist 才是本地主权边界。

当前版本面向单机作品演示和内部原型。Git worktree 提供任务文件隔离，但不是操作系统安全沙箱。含恶意测试代码的仓库仍可能使用宿主进程权限；生产部署必须增加禁网容器或微虚机、只读基础镜像、非 root 用户、CPU/内存/进程数限制和短期凭证。

## 已实现控制

| 威胁 | 已实现控制 | 失败方式 |
|---|---|---|
| `../`、绝对路径、符号链接越界 | 路径规范化并用 `relative_to` 证明仍在 worktree；repo mapper 同样拒绝外链 | `DENY` 或跳过 |
| 模型越过改动范围 | `allowed_paths` 与 `max_changed_files` 在 patch 执行前和最终完整 diff 审查时由宿主重复校验；完整 diff 覆盖 staged、unstaged 与 untracked | `DENY` 或审查失败 |
| shell 注入 | 命令只接受 JSON 参数数组，`shell=False`，拒绝 shell 标记与内联 Python | `DENY` |
| 网络和破坏性命令 | 拒绝网络可执行文件、shell、`rm` 和危险 Git 子命令 | `DENY` |
| 未知命令 | 未命中完整参数元组 allowlist | `ASK`，等待人工审批 |
| 伪造、陈旧或旁路审批 | `ASK` 立即暂停循环；`request_id` 和参数 SHA-256 必须匹配；精确请求与脱敏事件分层存储，批准后由宿主解密、校验并执行 | `409`、继续等待或安全停止 |
| MCP 工具混淆 | `mcp__server__tool` 命名空间加操作员精确 allowlist | `DENY` |
| 重试造成重复副作用 | 操作前唯一 claim、`operation_id`、参数摘要、SQLite 事务 | 重放已提交结果；未知结局停止核对 |
| 两个执行者同时推进 | `BEGIN IMMEDIATE`、任务序列号和 `expected_sequence`；已有未完成 claim 返回 409，不把任务误写为失败 | 并发冲突或复用 claim |
| 密钥进入 API/轨迹/审计事件 | 输入、工具参数、结果、错误和状态事件在持久化前脱敏；待审批精确请求只进入 Fernet 密文列 | 保存替换后的审计文本；密钥或密文异常时停止 |
| 补丁因脱敏失真 | 审计 diff 保存脱敏文本及原始 SHA-256；本地 patch 重新读取并校验完全相同的 SHA-256 | 不生成不一致证据包 |
| 无限循环、读取或输出爆炸 | 从任务创建时刻计算的持久墙钟、持久步数、重复调用、上下文压缩、流式总输出上限；文件读取限于 regular file 的 `max_bytes + 1`；剩余时间下传到工具进程与 MCP 等适配器 | 明确异常、截断、终止进程组或有界失败 |
| 模型虚报成功 | 宿主重跑固定测试并要求非空 Git diff | 不能进入成功终态 |
| 半写入产物 | 临时文件、`fsync`、同目录 `os.replace` | 保留旧完整文件或无文件 |

## 权限与数据边界

`OpenAIAgentsRuntime` 只把模型决策转换成 `AgentAction`，没有 worktree、SQLite 或进程执行器引用。即使提示注入要求 `run_command ["curl", ...]`，请求仍要经过本地 `PolicyEngine`。MCP 发现也不代表授权：适配器保留服务器来源并生成命名空间名称，策略要求完整名称精确命中 allowlist，远端结果之后仍经过统一网关。

审批不是一个可复用布尔开关。策略返回 `ASK` 后，Agent loop 立即结束本轮，不再允许模型提出第二个副作用。service 从最后一条 pending 事件读取 `operation_id` 和 `arguments_digest`，批准事件同时绑定两者。审计事件只含脱敏参数；完整 `ToolRequest` 使用数据库同目录的 Fernet 密钥加密后写入 `pending_approvals`，密钥创建权限为 `0600`。恢复时宿主解密请求并重新核对 operation ID 与参数摘要；claim 执行权时在同一事务删除 pending 密文。参数变化、密钥缺失、权限过宽、密文损坏或摘要不一致都会安全停止。重复提交同一份批准是幂等的，其他 request ID 会被拒绝。

`constraints` 是给模型看的自然语言建议，不能作为安全声明。需要强制执行的改动范围使用 `allowed_paths` 和 `max_changed_files`：策略引擎先解析 unified diff 的文件头，在 `git apply` 前验证 worktree 包含关系、glob 范围和文件数；最终审查以 `HEAD` 为基线读取 staged 与 unstaged tracked diff，再逐个纳入未跟踪文件并重复范围检查。三类变更共享同一个 256 KB 与 15 秒总预算，任何命令失败或截断都 fail closed。API schema 明确区分 advisory 与 host-enforced 字段。

审计数据与发布补丁使用不同披露等级。SQLite、SSE、JSONL 轨迹和测试输出保存脱敏值；`change.patch` 则必须保留可应用的精确字节，所以被标记为 `access-controlled-exact`，只落在本地证据目录，API 与 React 页面只显示路径元数据、改动文件和哈希，不返回 patch 正文。源码本来包含秘密时，精确 patch 仍可能包含它，必须按敏感工件控制目录权限、上传范围和保留期限。

OpenTelemetry 默认只导出标识符和布尔、整数、浮点指标。prompt、Issue、工具参数、输出和错误正文不作为 span 属性。本地 JSONL 即便已脱敏，也仍可能含业务名称，不能当作公开日志无条件上传。

## 两个崩溃窗口

第一个窗口是“结果已提交，任务状态尚未继续”。网关在一个 SQLite 写事务中保存脱敏结果、终结事件和快照；恢复时相同 `operation_id + arguments_digest` 返回旧结果并写 `tool.replayed`，工具不会再次执行。内置 benchmark 的故障注入覆盖这个窗口。

第二个窗口更危险：工具已经产生副作用，但结果还没来得及提交。RepoPilot-R 在调用工具之前，先原子写入 `operation_claims` 唯一记录和 `tool.started` 事件。若此后进程消失，恢复会看到“有意图、无结果”，抛出 `OperationInProgress` 并停止自动执行。系统不假装知道副作用是否完成，也不会冒险再执行；操作员核对工作树后才能决定补偿或人工提交结果。集成测试专门模拟 effect-after-claim / before-commit。

这提供的是单机操作身份下的 effectively-once 与 fail-stop 语义，不是分布式 exactly-once。MCP 适配器用 daemon 线程和有界队列保证本地等待会在任务截止时间返回失败，但 Python 无法强制取消任意阻塞的第三方 transport；远端调用可能稍后完成。远端 MCP 副作用仍需传递 idempotency key，或为高风险操作设计取消、查询、补偿与人工审批协议。

## 进程边界

`ProcessRunner` 不经 shell，环境只保留少量运行键。stdout 与 stderr 由两个线程流式读取，共享一个字节预算，所以子进程持续输出不会让父进程无界缓存。任务超时从 `created_at` 开始累计，恢复不会重置；runtime 返回后会再检查一次，剩余秒数继续下传给命令、搜索、patch 校验和 diff 工具。每个进程创建新 session；超时先向整个进程组发送 `SIGTERM`，无论父进程是否先退出，都会再对仍存在的进程组发送 `SIGKILL`，避免忽略 TERM 的孙进程留在后台。当前实现面向 macOS/Linux，Windows 生产适配需要替换为 Job Object 或容器执行器。

## 明确未覆盖

- 没有容器、seccomp、微虚机或网络命名空间；恶意仓库测试不应在生产宿主直接运行。
- 没有多租户认证、RBAC、审计签名、集中密钥管理和不可变对象存储。
- 本机 Fernet 密钥与数据库同盘，解决的是审计误泄露和静态密文分层，不替代 KMS、磁盘加密、密钥轮换或备份恢复方案；数据库备份必须连同受控密钥备份。
- 脱敏器基于已知密钥与常见 token 形态，不是完整 DLP。
- MCP 连接未实现 OAuth、证书固定、动态客户端注册策略、transport 级强制取消或企业安全网关。
- API 默认绑定 loopback 且没有用户认证；使用 `--host 0.0.0.0` 前必须放到受控反向代理之后。
- PR 只生成草稿，不会 push 分支或创建真实远程 PR，最终写操作仍由人或上层 CI 完成。

## 生产加固顺序

第一步将 `ProcessRunner` 换成禁网容器/微虚机执行器；第二步接企业 OIDC、仓库 allowlist 和短期 GitHub App token；第三步把审批请求接入 Slack、Teams 或工单并记录审批者身份；第四步将轨迹和工件写入加密、不可变对象存储并设置留存策略；第五步做提示注入、路径穿越、恶意测试和 MCP confused-deputy 红队测试。

自动化证据集中在 `tests/unit/test_policy.py`、`tests/integration/test_tool_gateway.py`、`tests/integration/test_process.py`、`tests/e2e/test_recovery.py` 和 `tests/e2e/test_issue_to_patch.py`。完整结果见[评估说明](EVALUATION.md)与[最终验证报告](VERIFICATION_REPORT.md)。
