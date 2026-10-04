# RepoPilot-R

RepoPilot-R 是一个面向代码仓库的可靠性优先软件工程 Agent。输入仓库、Issue 和允许执行的测试命令后，它在独立 Git worktree 中建立代码地图，驱动有界工具调用循环，应用补丁并执行确定性验证，最后产出 patch、测试报告、完整事件轨迹和 PR 草稿。模型负责提出动作，宿主程序保留权限、状态、执行和验收权。

这个仓库不是聊天界面套壳。它专门解决软件 Agent 最容易被忽略的工程问题：越权工具调用、重复副作用、进程崩溃后的重放、上下文无限增长、结果无法审计，以及“模型说已修好”却没有测试证据。

## 已验证结果

2026-08-24 在 Python 3.13.13、macOS arm64 上运行完整验证脚本，得到以下结果：

- 133 个 Python 测试通过，分支覆盖率 85.83%
- Ruff 与严格 mypy 检查通过
- 3/3 个内置离线修复任务通过隐藏测试和改动范围检查
- 故障注入场景完成 1 次恢复，0 次策略违规
- React 仪表盘 2 个组件测试、TypeScript 类型检查和 Vite 生产构建通过

这 3 个任务是仓库内置的确定性 fixture，不是 SWE-bench 成绩。原始结果的归一化快照和校验值见[评估说明](docs/EVALUATION.md)。

## 架构

```text
CLI / FastAPI v1 / SSE / React 19 dashboard
                    |
          TaskApplicationService
                    |
        DurableTaskOrchestrator
     state machine + SQLite event log
                    |
             MinimalAgentLoop
                    |
            SafeToolGateway
 policy -> durable claim -> tool -> redaction -> commit
                    |
 Git worktree / repo map / patch / tests / artifacts
```

核心后端使用 Python 3.12+、Pydantic 2、SQLite、FastAPI、Typer、Tree-sitter 和 ripgrep。前端使用 React 19、TypeScript 5.9、Vite 7 与 Vitest。可选适配层支持 OpenAI Agents SDK、MCP 2 和 OpenTelemetry；离线核心不需要 API Key，也不把 Docker 当作本地演示的前置条件。

## 五分钟运行

```bash
python3.13 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
npm --prefix web ci
.venv/bin/repopilot doctor
.venv/bin/repopilot demo --workspace /tmp/repopilot-portfolio --json
sh scripts/verify.sh
```

`demo` 每次要求新的工作目录，防止旧证据被覆盖。想看 API 与网页界面时，打开两个终端：

```bash
.venv/bin/repopilot serve \
  --workspace /tmp/repopilot-portfolio/demo-run/cases/config_validation
npm --prefix web run dev
```

浏览器访问 `http://127.0.0.1:5173`。Vite 会把 `/v1`、`/health` 和 `/ready` 代理到本地 API。接口文档位于 `http://127.0.0.1:8000/docs`。

这里的 API 与网页直接读取刚才 demo 生成的同一份 `state.db`、worktree 和四件套证据，不会启动一个空白工作区。`demo --json` 也会在 `showcase.workspace` 中打印这个准确路径。

## 交付导航

- [项目说明与架构](docs/PROJECT_GUIDE.md)
- [重要代码逐行解析](docs/CODE_WALKTHROUGH.md)
- [项目简介与中英文简历写法](docs/PROJECT_BRIEF_AND_RESUME.md)
- [业务对接与落地路径](docs/BUSINESS_INTEGRATION.md)
- [安全边界与威胁模型](docs/SECURITY.md)
- [评估口径和可复现结果](docs/EVALUATION.md)
- [演示脚本](docs/DEMO.md)
- [技术调研来源](docs/SOURCES.md)
- [最终验证报告](docs/VERIFICATION_REPORT.md)

项目采用 MIT License 元数据。当前实现不会自动推送远程分支或创建真实 PR；它只生成可审核的 PR 草稿和证据包，把最终发布动作留给人或上层 CI。
