# RepoPilot-R 项目简介与简历材料

## 一句话介绍

RepoPilot-R 是一个可靠性优先的软件工程 Agent：它把仓库 Issue 转成经过测试的 patch，并用权限网关、事务事件日志、幂等重放和 Git worktree 解决越权、崩溃恢复与结果不可审计的问题。

## 30 秒项目介绍

很多代码 Agent 能调用工具，但它们在进程中断、重复请求和权限控制上很脆。我做了 RepoPilot-R，让模型只负责提出下一步结构化动作，宿主程序掌握每一次文件和命令操作。任务在独立 worktree 里运行；SQLite 在副作用前保存操作意图，在结果返回后原子提交脱敏结果、事件和快照。已提交结果可以安全重放，只有意图而没有结果的操作会停止等待人工核对，不会盲目再执行。成功也不是模型自己宣布，必须重跑允许的测试、满足宿主侧改动范围约束并取得覆盖 staged、unstaged、untracked 的非空 Git diff。当前完整验证有 133 个 Python 测试，分支覆盖率 85.83%；内置 benchmark 完成 3/3 个任务、1 次故障恢复、0 策略违规。

## 核心技术栈

后端：Python 3.12+、Pydantic 2、SQLite 事务、Fernet 加密、FastAPI、SSE、Typer、Tree-sitter、ripgrep、Git worktree、pytest、mypy、Ruff。

Agent 与协议：有界 tool-calling loop、结构化输出、paired context compaction、idempotency key、event log、human approval、可选 OpenAI Agents SDK / GPT-5.6 决策适配器、MCP 2 命名空间适配器、JSONL 与 OpenTelemetry。

前端与交付：React 19、TypeScript 5.9、Vite 7、Vitest、GitHub Actions、Python 3.12/3.13 与 Node 24 验证矩阵。

## 技术亮点

### 1. 宿主控制的 Agent，而不是把权限塞进 prompt

模型输出只有 `tool` 或 `final` 两种形状。文件路径先做规范化和 worktree 包含性证明；命令必须是参数数组。shell、网络工具、破坏性 Git 和内联 Python 直接拒绝，未知命令暂停到人工审批。新增工具默认无权限。

### 2. 能证明崩溃后不会重复执行同一工具

每次工具调用带稳定 `operation_id` 和参数 SHA-256。网关先在 SQLite 原子写入唯一 claim 与 `tool.started`，再调用工具；结果、终结事件和任务快照随后在一个写事务中提交。故障注入测试覆盖两种窗口：已提交结果可在重启后 `tool.replayed`，副作用后但结果提交前的未知结局则被 claim 拦住并停止核对，两者都不会自动重复调用工具。

### 3. 模型答复不决定成功

编排器把 `final` 当作执行阶段结束。随后宿主重新运行预先允许的测试命令，再读取二进制安全 Git diff。测试或审查任一步失败，任务都不能进入 `SUCCEEDED`。最终工件来自持久化证据，不从聊天文本拼结果。

### 4. 离线可复现，在线能力可替换

三组 fixture 使用脚本化 runtime，稳定覆盖补丁、隐藏测试、改动范围和恢复。在线 OpenAI Agents SDK 只替换决策层，MCP 只扩展工具来源，两者都不能绕过策略和验收主链路。

## 中文简历版本

**RepoPilot-R｜可靠性优先的软件工程 Agent｜个人项目**

Python, FastAPI, Pydantic, SQLite, Git Worktree, Tree-sitter, OpenAI Agents SDK, MCP, React, TypeScript

- 设计并实现 Issue-to-patch Agent 控制面，将模型决策与工具执行拆分；以 fail-closed 策略、路径包含校验、命令参数 allowlist 和人工审批约束文件、进程与 MCP 工具能力。
- 基于 SQLite 事务事件日志、操作前 claim 与 `operation_id` 幂等键实现断点恢复；故障注入覆盖已提交结果重放和未知结局拦截，完成 1 次自动恢复且未重复副作用。
- 建立隐藏测试、完整工作树范围检查和工件哈希组成的离线评估，3/3 任务通过、0 策略违规；133 个 Python 测试与 85.83% 分支覆盖率通过，配套 FastAPI/SSE、React 证据面板和 Python 3.12/3.13 CI。

如果版面只能放两条，保留第二和第三条。它们分别证明你能做可靠后端和能用数字验收 Agent。项目标题右侧放 GitHub 链接；不要在简历上写“SWE-bench 100%”或“生产级沙箱”。

## English resume version

**RepoPilot-R | Reliability-first software engineering agent | Personal project**

Python, FastAPI, Pydantic, SQLite, Git Worktree, Tree-sitter, OpenAI Agents SDK, MCP, React, TypeScript

- Built a host-controlled issue-to-patch agent with fail-closed tool policy, worktree path containment, exact command allowlists, and approval gates for local and namespaced MCP tools.
- Implemented crash-safe resume with a transactional SQLite event log, pre-effect operation claims, and idempotent operation IDs; fault injection verifies committed-result replay and fail-stop handling of indeterminate outcomes without automatic duplicate effects.
- Created a deterministic evaluator that grades hidden tests, complete-worktree path/file-count scope, and artifact hashes. Results: 3/3 fixtures resolved, zero policy violations, 133 Python tests passing, and 85.83% branch coverage. Shipped a FastAPI/SSE API, React evidence dashboard, and Python 3.12/3.13 CI matrix.

## HR 为什么会停下来

标题先说“software engineering agent”，HR 能立刻归类岗位。第二行出现 Python、FastAPI、Agent SDK、MCP、React，能命中当前 Agent/后端实习关键词。三条 bullet 没有“参与”“了解”或抽象形容词，开头都是你真正做过的设计与实现。

数字也有出处。3/3 表示仓库内三组 fixture，不伪装成公开榜单；85.83% 来自分支覆盖率；0 策略违规来自评估事件。面试官点进仓库后可以运行同一个脚本复核，简历、README 和 JSON 报告使用同一口径。

## 技术面试讲法

先画一条链：runtime 提议动作，gateway 做权限和幂等，tool 在 worktree 执行，SQLite 保存结果，orchestrator 重跑测试并生成工件。然后讲故障窗口。工具可能已经改文件，进程却还没推进任务状态；普通重试会再改一次。你的处理是给动作稳定身份，把结果与事件原子提交，恢复时先查操作表。

面试官若追问 exactly-once，直接说明这是本地操作身份下的 effectively-once。远端 MCP 若完成副作用但丢失响应，本地无法确认，必须把幂等键传到远端或增加补偿事务。这个回答比声称“完全 exactly-once”更可信。

面试官若追问为什么不直接用 Docker，说明作品的离线主路径要低门槛可复现，所以本地以 worktree 加策略做能力边界；你没有把它叫安全沙箱。业务部署会替换 `ProcessRunner` 为禁网容器或微虚机，而领域协议、operation ID 和验收逻辑保持不变。

## 可回答的追问

“为什么用 SQLite？” 单机任务需要事务、唯一键和可查询事件，SQLite 足够小，也能展示一致性。多 worker 部署时迁移到 Postgres，保留 `expected_sequence` 和 operation 唯一约束。

“为什么要 Tree-sitter 加 AST？” Tree-sitter提供带行号的符号结构并允许未来扩语言；Python AST 提取 import 简单可靠。ripgrep按 Issue 词项补相关片段，最终统一受字符预算限制。

“怎么控制成本？” 当前有步数、墙钟、重复调用、上下文和进程输出预算。接在线模型后再为 runtime 增加 token 与美元上限，并把成本写到数字遥测。

“怎么证明没有泄密？” 只能证明已实现的控制：已知密钥和常见 token 在工具结果持久化前脱敏，OTel 不导出 prompt 或工具正文。它不是完整 DLP；敏感仓库仍需私有模型端点和数据保留策略。

## GitHub 首页排布

仓库置顶后，README 首屏保留一句话、架构图、四个真实数字和五分钟命令。演示 GIF 可以后补，但不能替代可运行脚本。`docs/CODE_WALKTHROUGH.md` 让技术面试官看到关键代码不是生成后无人理解；`docs/BUSINESS_INTEGRATION.md` 让招聘方看到你能把 Agent 放进真实研发流程。

提交历史已经按领域、存储、安全、worktree、repo map、网关、Agent、编排、评估、CLI、API、适配器、UI 和验证拆分。面试时挑“safe gateway”“durable orchestrator”“evaluation”三个提交讲，能展示从设计到测试的工程节奏。

评估口径和报告校验值见[评估说明](EVALUATION.md)，逐行代码证据见[重要代码解析](CODE_WALKTHROUGH.md)，业务落地见[对接方案](BUSINESS_INTEGRATION.md)。
