# ForgePilot 真实仓库端到端验收报告

验收日期：2026-10-23（Pacific/Auckland）；后台 UTC 完成于 2026-10-22 11:15:19。

## 结论与口径

发布前追加复验（同日）：修复公开导入/浅克隆缺失历史基线时的准备流程，优先使用固定源码 bundle，仍校验原始基线提交；新增一项参数化回归。真实 MySQL 全量 **340 passed、1 SQLite 专属 skip，综合覆盖率（含分支）87.95%，66.11 秒**。下表保留此前真实模型闭环完成时的 339 / 87.97%，两次记录不覆盖、不混算。

真实 DeepSeek → 后台任务 → 隔离 Git worktree 修改 → MySQL 持久化 → 宿主独立验收 → 四类证据工件的闭环已通过。此次是**八轮、带操作反馈和检查点续修的工程验证**，不是一次零干预成功。没有远程 push、PR 发布或云部署。

| 验证范围 | 实际结果 |
| --- | --- |
| 最终在线作业 | SUCCEEDED，10 代理步骤，最后一轮无审批等待 |
| 模型验收 | 32 项特性 PASS + 236 项候选仓库测试 PASS |
| 宿主独立验收 | 相同冻结命令，32 + 236 PASS，退出码 0 |
| 完成后再次独立复测 | 32 + 236 PASS，48.75 秒，退出码 0 |
| 交付项目全量测试 | 339 passed，1 SQLite 专属 skip，真实 MySQL ENABLE=1 |
| 当前源码综合覆盖率（含分支） | 87.97%，门槛 85% 保留 |
| 前端 | 11 tests、TypeScript、production build PASS |
| 静态检查 | Ruff、mypy 49 文件、diff whitespace PASS |
| 离线评估 | scripted-offline 3/3，0 policy violations；不代表模型成功率 |

候选仓库来自历史基线，测试数量与最新宿主不同，236 和 339 不应相加或互相替代。

## 可追溯身份

- job：`6279297f-4707-48fd-bed9-46aa8ad859b4`
- task：`403efb67-e149-487a-8571-bb0e95446aff`
- 模型：DeepSeek `deepseek-flash`，真实 API，非 scripted provider。
- 数据库：本机隔离 MySQL 9.6，`forgepilot_live`；测试使用 `forgepilot_test`。只读复核成功任务有 28 条事件、11 条工具记录。
- 原始基线：`be2a6e58e961c2b2945f564b85cdd52d1667cd7a`
- 未通过检查点序列：`b8c650bbad480783c66b55f9603aea156d0af2dc` → `ff761c674e37cc7b204869085a6cfdf6c3920081` → `1efecddc8037d61776b4c3bcfe5556ab15e31f25`
- 最终 worktree：`<E2E_ROOT>/workspace/worktrees/403efb67-e149-487a-8571-bb0e95446aff`
- 原始候选及检查点保留；从未人工编辑正在运行的模型候选。通过后才把审查过的累计变更合入交付项目。

## 冻结验收与工件

命令使用明确 argv：解释器 `<PROJECT_ROOT>/.venv/bin/python`，脚本 `<E2E_ROOT>/acceptance.py`，cwd 为最终 worktree。脚本位于仓库外、只读，功能通过后才执行整个候选 tests；失败/超时返回非零。只读位不等同于恶意代码沙箱。

验收 v3 SHA-256：`a87daa2b32a16b58748bf2dfe782c2e815d2d696df85d1d354ac5fc84bca3675`。

工件目录：`<E2E_ROOT>/workspace/artifacts/403efb67-e149-487a-8571-bb0e95446aff`。以下均重新读取实际文件并与 API 摘要核对一致：

| 文件 | SHA-256 |
| --- | --- |
| change.patch | fb0f4af3c63a9c4637eb232b2442907e90132f920b1e5e5be11f55d58ab39d84 |
| test-report.json | 1009e213647eeea94b6646b05ab5337f2ef4367e458796bce43f8fd2e919fe9a |
| trajectory.jsonl | 6a07392c06fb550c1589053115faf84e7928308d05cf0b6065771ff97048f879 |
| pull-request.md | cc6810858d3b664230811bf18831d71a6f825cba100df65192a99caf0a02552d |

change.patch 是最后任务相对第三检查点的增量，不是完整特性补丁。最终候选相对原始基线的 `git diff be2a6e58e961c2b2945f564b85cdd52d1667cd7a --` 为 15899 字节，SHA-256 `09fb48b125cf98c90d8c43e7e6a46c238a012fc71177ce124188da7320d18259`，涉及 builtin.py、catalog.py、test_process.py、test_connected_cli.py。

## 修复与安全边界

文件头前 8192 字节的 NUL 检查独立于 limit/offset，EOF 和极大 offset 不能绕过；目标页 NUL 同样拒绝。此规则不是扫描整文件的通用二进制分类器。整数/null/bool、字节截断、目录/符号链接、时间和文件大小边界保持。

无必要的未授权文本读取命令会被拒绝并引导内置工具，不是自动放行。重复计数仅在首次成功 patch 后对宿主精确验收命令重置；缓存 patch 重放、失败 patch、其他命令均不重置，总步数和时限不增加。

所有测试子进程继续过滤凭据。工作目录隔离、命令策略和环境过滤不等于 OS 沙箱；当前只适合个人可信仓库，不能直接暴露成匿名编程服务。

## 文档与复验入口

- 全部可恢复问题、失败尝试与 debug 过程：[DEBUG_JOURNAL.md](DEBUG_JOURNAL.md)。
- 不依赖 Mac 的服务器架构及上线门槛：[CLOUD_DEPLOYMENT_PLAN.md](CLOUD_DEPLOYMENT_PLAN.md)。
- 全量本机验证入口：`scripts/verify.sh`；真实 MySQL 需显式设置测试 opt-in 和安全注入凭据，否则会跳过部分集成测试。不要把默认跳过说成真实数据库验证。
- 当前 UI 父进程未强制重启；下次日常使用前正常重启原启动脚本，Key 只在本机隐藏输入。新 worker 已使用修复过的源码完成本次任务。
