# ForgePilot 0.3 验证记录

日期：2026-10-22；本机 macOS arm64 / Python 3.13.13。

完整运行 `sh scripts/verify.sh`，退出码 0：

- Python：298 passed、5 skipped；语句与分支合计覆盖率 87.29%。
- Ruff 与严格 mypy 通过（49 个源码文件）。
- 离线 benchmark：3/3 通过、1 次故障恢复、0 策略违规。
- React/Vitest：11 项通过；TypeScript 检查与 Vite 生产构建通过。
- 原始离线报告：`/private/tmp/repopilot-verify.Dv3Yrd/evaluation/evaluation-results/report.json`。

交付目录 `<PROJECT_ROOT>` 已建立独立 `.venv` 并安装声明依赖；该目录复测 298 passed、5 skipped。额外修正测试脚本中带空格解释器路径的引用方式。实际从交付目录启动网页、提交 Demo，观察 SUCCEEDED 与测试 PASS；[工作台截图](generated/forgepilot-demo.png)。

真实仓库验收已准备在 `<E2E_ROOT>`，其中 `manifest.json` 保存绝对命令、基线提交与验收哈希，`task.md` 可直接用于在线任务。旧文件备份位于 `<LOCAL_BACKUP>`。临时预览服务在验收后停止，重新启动见快速开始。

## 已验证范围

- 真实独立 subprocess 执行 fixture、持久化任务并生成补丁/测试报告/轨迹/PR 草稿。
- 本地 HTTP 模拟模型驱动主代理与独立分析子代理；精确审批后继续执行，审批等待中可取消。
- Textual 交互测试覆盖输入、轮询、选择、网络延迟、审批、取消、恢复和未创建任务的事件 404。
- React 操作面板覆盖提交、状态刷新、失败、审批、取消、恢复、四类产物、离线说明与旧审计兼容。
- 浏览器实际提交 Demo，观察任务 SUCCEEDED、宿主测试 PASS 与补丁预览。
- 独立审查后新增回归：搜索选项注入、旧进程覆盖新状态、原子 PID 声明、恢复交接、延迟 PID 探测、审批取消竞态、父任务取消后禁止派生。
- E2E 准备：固定真实项目提交，独立 clone，外部只读验收脚本与 SHA-256；19 项基线中 3 项通过、16 项分页相关预期失败。准备过程不调用模型。

## 不应混淆的边界

模型 HTTP 测试使用本地预设回复，不是付费供应商模型实测；Demo 是固定脚本，不会根据自由输入自主编程。真实模型 E2E 目前是准备完成，等待使用者在本机配置凭据并显式提交。

MySQL 适配与配置已实现，默认契约测试使用 SQLAlchemy/SQLite。4 项真实 MySQL 测试未启用，另 1 项因 SQLite 不能证明 MySQL 行锁而跳过；没有验证真实数据库连接、InnoDB 并发或 TLS 握手。未更改用户现有数据库。

后台队列元数据仍在本地 SQLite，MySQL 不是分布式 scheduler；取消为协作式，已有网络请求/命令可能运行到超时或下一安全边界。固定测试命令运行可信仓库代码，worktree 和工具策略不是 OS 沙箱。不得将本项目写成完整复刻 CC/Codex、生产多租户平台或公开 benchmark 成绩。

覆盖率仅统计当前 `src/repopilot`；外部验收用的旧版本 clone 不应被纳入当前项目分母。保留 85% 分支覆盖门槛，没有降低验收阈值。
