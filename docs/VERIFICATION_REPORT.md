# RepoPilot-R 最终验证报告

## 验证对象

验证对象是 `feature/repopilot-r` 的最终候选实现，日期为 2026-08-24。统一入口为 `sh scripts/verify.sh`，它不会读取文档中的手写成功声明，而是依次运行静态检查、后端测试与分支覆盖率、三例离线 benchmark、前端测试、TypeScript 类型检查和生产构建。

## 已执行结果

| 检查 | 实际结果 | 证据含义 |
|---|---|---|
| Ruff | passed | 后端源码与测试无 lint 错误 |
| strict mypy | passed, 36 source files | 领域、应用与适配器类型边界通过 |
| pytest | 133 passed | 单元、集成、API、CLI、恢复、文档与端到端测试通过 |
| branch coverage | 85.83% | 高于脚本固定的 85% 门槛 |
| offline benchmark | 3/3 resolved | 三例均通过隐藏测试和改动范围检查 |
| recovery | 1 | 已提交操作在重启后安全重放 |
| policy violations | 0 | benchmark 中无策略违规事件 |
| Vitest | 2 passed | 仪表盘恢复与空状态通过 |
| TypeScript | passed | `tsc --noEmit` 通过 |
| Vite build | passed | React 生产 bundle 成功生成 |

生成报告位于 [evaluation-report.raw.json](generated/evaluation-report.raw.json)，其 SHA-256 是 `d03e61ac52ebf601a28c058dffae12727bf9fe62d9769764cfbe03aa9e5aa50b`。报告不含本机解释器路径或用户目录；归一化简历口径位于 [evaluation-report.json](generated/evaluation-report.json)。

## 洁净克隆复验

包含全部运行时代码的候选提交 `e3777b4817a2fc2a04c491a44f001185cd183817` 已在临时目录重新克隆，新建 Python 3.13 虚拟环境并从包索引安装 `.[dev]`，再执行同一份验证脚本。洁净克隆得到 133 passed、85.83% branch coverage、3/3 resolved、1 次恢复、0 策略违规，以及 Vitest、TypeScript、Vite 全部通过。当前机器使用 Node 26.0.0 与 npm 11.12.1；npm 11.12.1 的 `npm ci` 仍以自身的 `Exit handler never called` 错误退出，因此前端复验使用与 lockfile 完全匹配、且已在开发工作树通过全量验证的依赖树。前端源码、测试与生产构建全部来自洁净克隆；这一环境限制不被表述为从零 npm 安装成功。

## 关键需求到证据映射

| 需求 | 自动化证据 |
|---|---|
| 操作结果可重放且不重复副作用 | `tests/e2e/test_recovery.py` |
| 副作用后、结果提交前不盲目重试 | `tests/integration/test_tool_gateway.py` 的持久 claim 故障测试 |
| 重启保留历史、剩余步数和创建时刻截止时间 | `tests/e2e/test_recovery.py` 的上下文与持久墙钟测试 |
| 审批立即暂停并跨重启执行准确请求 | `tests/e2e/test_recovery.py` 的审批生命周期测试，以及 API/CLI 测试 |
| 敏感审批参数不进入事件或数据库明文 | `tests/e2e/test_recovery.py` 的加密 pending 与跨重启测试 |
| 并发 claim 不误写任务失败 | `tests/e2e/test_recovery.py` 的 `OperationInProgress` 冲突测试 |
| 路径与最大文件数由宿主硬执行 | `tests/unit/test_policy.py` 与 `tests/e2e/test_recovery.py` 的结构化范围测试 |
| staged、unstaged 与 untracked 都进入最终范围和工件 | tool gateway、recovery 与 issue-to-patch 的完整 diff 测试 |
| 空 diff 不能成功 | `tests/e2e/test_issue_to_patch.py` |
| 路径和 symlink 不逃逸 worktree | policy、repository mapper 与工具集成测试 |
| 持久截止时间截断工具，超时终止所有子孙进程且输出有界 | `tests/e2e/test_recovery.py`、`tests/integration/test_process.py` |
| 大文件读取不整体载入，MCP 本地等待有截止时间 | process 与 optional adapter 的有界失败测试 |
| API、SSE、React 展示同一 demo 数据 | CLI showcase、API artifact 与 App 组件测试 |
| 精确 patch 不被脱敏替换破坏 | `tests/e2e/test_issue_to_patch.py` 的原始摘要校验测试 |
| 报告可公开且不泄漏机器路径 | `tests/e2e/test_offline_benchmark.py` |

## 结论与边界

当前证据支持“可靠性优先的单机软件工程 Agent 原型”这一项目定位：状态、工具权限、操作身份、恢复、确定性验收和证据包均有可运行实现。它不支持“生产安全沙箱”“分布式 exactly-once”“SWE-bench 成绩”或“无人值守自动合并”等更强声明。生产落地仍需容器/微虚机、认证与 RBAC、远端幂等协议、集中工件存储和更大规模评估集。

复现步骤、评测限制和逐 case 数据见[评估说明](EVALUATION.md)；关键代码的精确行号见[重要代码解析](CODE_WALKTHROUGH.md)。
