# RepoPilot-R 项目说明

## 1. 要解决的问题

让大模型改代码并不难。真正难的是回答五个工程问题：它能碰哪些文件和命令，重复请求会不会执行两次，进程中断后从哪里继续，怎样证明测试确实跑过，以及人如何重放整个决策过程。RepoPilot-R 把这些问题放在模型能力之前，形成一条从 Issue 到可审查 patch 的本地流水线。

系统接收 `TaskSpec`：仓库路径、Issue、测试命令参数数组、自然语言建议、宿主侧允许路径/最大改动文件数，以及步数和墙钟预算。任务创建后，状态只能沿显式状态机前进。每次状态变化和工具结果写入 SQLite；代码修改发生在专用 Git worktree；成功必须同时满足允许的测试命令成功、最终 diff 非空且改动范围合规。完成后生成四件证据：`change.patch`、`test-report.json`、`trajectory.jsonl`、`pull-request.md`。

## 2. 设计原则

第一，模型不是权限主体。它只能返回结构化 `AgentAction`，所有工具都经过 `SafeToolGateway`。策略引擎根据真实工具名、路径和参数判断 `ALLOW`、`ASK` 或 `DENY`，不采信模型对动作的文字描述。

第二，副作用有身份和执行权。每个 `ToolRequest` 都带 `operation_id` 和参数摘要。SQLite 在工具执行前原子写入唯一 claim 与 `tool.started`；结果返回后再原子写入脱敏结果、终结事件和任务快照。相同身份的已知结果可重放；只有 claim 没有结果时停止自动执行，交给人工核对。相同身份换参数会触发冲突。

第三，测试是终态门槛。Agent 的自然语言 `final` 只会把任务送入 `TESTING`，不会直接标记成功。宿主重新执行预先允许的参数数组，再读取 Git diff；任何一步失败都会进入 `FAILED`。

第四，离线主路径必须可复现。默认演示使用仓库内的三组 fixture 和脚本化动作，不需要 API Key。OpenAI Agents SDK、MCP 和 OpenTelemetry 位于可选适配层，缺少额外依赖时不会破坏核心路径。

## 3. 模块边界

| 层 | 目录 | 责任 |
|---|---|---|
| Domain | `src/repopilot/domain` | 不可变模型、任务状态、端口协议 |
| Agent | `src/repopilot/agent` | 有界循环、上下文压缩、脚本与模型运行时 |
| Tools | `src/repopilot/tools` | 策略、脱敏、内置工具、代码地图、统一网关 |
| Application | `src/repopilot/application` | 任务编排、恢复、审批和产物触发 |
| Infrastructure | `src/repopilot/infrastructure` | SQLite、Git worktree、进程、MCP、遥测、产物落盘 |
| Interfaces | `src/repopilot/api`, `src/repopilot/cli.py`, `web` | CLI、REST v1、SSE、React 证据仪表盘 |
| Evaluation | `src/repopilot/evaluation`, `benchmarks` | fixture 装载、隐藏测试、范围检查、报告生成 |

依赖方向从接口和基础设施指向应用与领域协议。`MinimalAgentLoop` 依赖 `Runtime` 和 `ToolGateway` 协议，不知道 SQLite、FastAPI 或某家模型 SDK。这个切面让脚本化运行时与在线模型共享同一条安全和验收路径。

## 4. 一次任务怎样运行

1. `TaskApplicationService.create` 生成任务 ID，并把带 `created_at` 的 `CREATED` 快照持久化；此刻开始计算不可因重启而重置的任务截止时间。
2. 编排器创建独立 worktree，任务进入 `INDEXING`。
3. `RepositoryMapper` 用 Git 文件清单、Tree-sitter Python AST 和 ripgrep 线索生成受字符预算约束的 `RepoMap`。
4. `MinimalAgentLoop` 让运行时提出一个工具调用或最终答复。每轮在模型调用前后检查剩余时间，并检查步数、取消信号、重复调用和上下文长度；工具只能拿到剩余预算。
5. `SafeToolGateway` 查已提交结果、做策略决策、原子 claim 执行权，之后才调用注册工具；结果脱敏后与事件、快照一起提交。
6. 若出现需要审批的操作，循环立即暂停，任务停在 `WAITING_APPROVAL`。批准同时匹配 pending 操作 ID 与参数摘要；公开事件只写脱敏参数，精确请求由本机 `0600` 密钥加密保存。恢复时宿主解密、校验并执行原请求，不依赖模型再次复述命令。
7. Agent 结束后，宿主执行指定测试命令。成功后读取包含已暂存、未暂存和未跟踪文件的完整 diff，重复检查 `allowed_paths` 与 `max_changed_files`，再计算审查摘要；任务才进入 `SUCCEEDED`。
8. `ArtifactBundleWriter` 从已持久化测试和 diff 摘要生成证据包；精确 patch 复用同一个完整 diff 实现，并同时核对原始 SHA-256 与结构化路径。

崩溃恢复不保存 Python 调用栈，而是重读任务快照、操作历史与总预算。编排器按状态重新进入当前阶段。已完成的 `operation_id` 不再触发真实工具；工具已开始但结果未知时，持久 claim 会阻止自动重试。前者安全重放，后者 fail-stop，两种窗口都不会盲目产生第二次副作用。

## 5. 技术栈为何这样选

Python 3.12+ 适合 Agent SDK、静态类型与本地自动化。Pydantic 2 把模型输出、API 请求和持久化边界统一为可验证的数据模型。SQLite 的 `BEGIN IMMEDIATE` 足够演示单机任务一致性，又能展示乐观序列号和原子提交。Git worktree 提供低成本任务隔离，不复制 `.git` 历史。Tree-sitter 提取稳定的符号结构，ripgrep 补充 Issue 词项相关片段。

FastAPI 和 SSE 暴露版本化状态与事件，不引入消息队列。React 19 仪表盘读取任务、事件和产物，页面在 1440px 与 390px 视口做过人工检查。GitHub Actions 在 Python 3.12/3.13 和 Node 24 上执行同一份 `scripts/verify.sh`，本地与 CI 的验收入口一致。

## 6. 当前边界

项目没有把 Docker 宣称为已实现的安全沙箱。Git worktree 是文件隔离，不是内核级隔离；命令安全依赖参数数组、可执行文件拒绝表、精确 allowlist 和超时。系统也不会连接 GitHub 后自动创建 PR。生产接入时应增加容器或微虚机执行器、OIDC 身份、仓库级配额、人工审批服务与远程工件存储，具体路径见[业务对接文档](BUSINESS_INTEGRATION.md)。

实现细节可从[重要代码逐行解析](CODE_WALKTHROUGH.md)继续阅读，运行方式见[演示脚本](DEMO.md)，安全假设见[安全说明](SECURITY.md)。
