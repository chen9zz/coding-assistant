# RepoPilot-R 演示脚本

这套演示分为 90 秒快速版和 8 分钟技术版。两种版本都从真实命令和持久化证据出发，不预录模型回答，也不依赖外部 API。

## 演示前准备

```bash
python3.13 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
npm --prefix web ci
.venv/bin/repopilot doctor
```

`doctor` 应显示 Python、Git、ripgrep 和 Tree-sitter 已就绪。Docker 与 OpenAI Agents SDK 标记为可选。每次运行都使用新的目录，例如把下列命令中的时间后缀换掉。

## 90 秒快速版

先执行完整离线演示：

```bash
.venv/bin/repopilot demo --workspace /tmp/repopilot-demo-20260824 --json
```

向面试官指出 JSON 中的 `resolved: 3`、`total: 3`、`policy_violations: 0` 和 `recovery_attempts: 1`。接着打开生成目录：

```text
/tmp/repopilot-demo-20260824/demo-run/evaluation-results/report.json
/tmp/repopilot-demo-20260824/demo-run/evaluation-results/report.md
```

同一 JSON 的 `showcase.artifacts` 还会列出 `change.patch`、`test-report.json`、`trajectory.jsonl` 和 `pull-request.md` 四份真实文件；`showcase.workspace` 则给出可直接启动 API 的工作区。

口述可以直接用这段话：

> 这是一个把 Issue 转成已验证 patch 的软件工程 Agent。模型只提议结构化动作，权限和验收由宿主控制。这里三组 fixture 都通过了隐藏测试，其中一组在补丁写入后注入崩溃。恢复时同一个 operation_id 被重放，但工具副作用没有执行第二次。最后的成功状态来自测试和 Git diff，不来自模型自报。

## 8 分钟技术版

### 1. 展示安全边界

打开 `src/repopilot/tools/policy.py`。说明路径先解析并证明仍位于 worktree 内；命令是 JSON 参数数组；shell、网络工具、破坏性 Git 子命令和内联 Python 被直接拒绝；测试命令采用精确参数数组 allowlist。其余未知命令进入人工审批，而不是默认放行。

### 2. 展示崩溃恢复

打开 `tests/e2e/test_recovery.py`。该测试在工具结果已持久化后抛出故障，再调用恢复。断言最终状态成功、补丁只出现一次、存储中只有一个操作身份，并出现 `tool.replayed` 审计事件。这里能体现事务、幂等键和事件溯源的组合价值。

### 3. 展示确定性验收

打开 `src/repopilot/application/orchestrator.py`。Agent 的 `final` 只进入 `TESTING`；宿主构造固定 `verify-{task_id}` 操作并重新执行允许的测试；成功后再读取 diff。只有两份证据都成功，状态才进入 `SUCCEEDED`。

### 4. 展示 UI

分别在两个终端运行：

```bash
.venv/bin/repopilot serve \
  --workspace /tmp/repopilot-demo-20260824/demo-run/cases/config_validation
npm --prefix web run dev
```

访问 `http://127.0.0.1:5173`。页面会展示同一个 demo 任务的状态进度、可按序重放的事件轨迹、恢复次数、策略拒绝、patch SHA-256、改动路径和测试证据元数据；出于安全边界，页面不渲染原始源码或 patch 正文。API 的 Swagger 页面在 `http://127.0.0.1:8000/docs`。

### 5. 收尾证据

```bash
sh scripts/verify.sh
```

这条命令顺序运行 Ruff、mypy、带分支覆盖率的 pytest、离线 benchmark、Vitest、TypeScript 类型检查和 Vite 生产构建。脚本会打印本次评估报告路径；它不会覆盖旧目录。

## 常见追问

“为什么不用任意 shell？” 因为字符串 shell 同时扩大命令注入和转义风险。RepoPilot-R 只接受参数数组，并把未知命令送去审批；批准必须同时匹配操作 ID 和参数摘要。

“这算安全沙箱吗？” 不算。Git worktree 解决任务文件隔离，策略网关限制工具能力。生产环境还要把进程执行放进容器或微虚机，限制网络、CPU、内存和系统调用。

“是否跑过 SWE-bench？” 没有。当前数字只来自仓库内三组确定性 fixture。评估设计支持替换 case loader，下一阶段再接 SWE-bench Verified 或企业内部 Issue 集。

“为什么还做脚本化运行时？” 它让状态机、策略、恢复和产物可以脱离模型波动进行回归测试。在线模型只是一个决策适配器，不能绕过同一条工具网关。
