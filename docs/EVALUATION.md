# ForgePilot 评估口径与可复现结果

当前新增模块见 [Agent / RAG 对照评估](PAIRED_EVALUATION.md)。下文是历史离线快照，不代表当前测试总数。

## 已验证快照

主快照于 2026-08-24 在 macOS arm64、Python 3.13.13 上生成。命令直接产出的报告保存在 [evaluation-report.raw.json](generated/evaluation-report.raw.json)，SHA-256 为 `d03e61ac52ebf601a28c058dffae12727bf9fe62d9769764cfbe03aa9e5aa50b`；简历和文档读取的归一化摘要位于 [evaluation-report.json](generated/evaluation-report.json)。报告环境块只记录 runtime、Python 与平台，不记录解释器、本机用户目录或工作区绝对路径。

| 质量门禁 | 结果 |
|---|---:|
| Python 测试 | 133 passed |
| 分支覆盖率 | 85.83% |
| Ruff | passed |
| strict mypy | passed |
| 内置任务 | 3/3 resolved |
| 故障恢复 | 1 |
| 策略违规 | 0 |
| React/Vitest | 2 passed |
| TypeScript | passed |
| Vite production build | passed |

Python 测试使用 `-W error::ResourceWarning`。覆盖率同时计入 statement 与 branch，门槛为 85%，当前精确值 85.83%，终端摘要四舍五入为 86%。133 个测试包含领域约束、加密审批参数跨重启恢复、并发 claim 冲突、持久墙钟、staged/unstaged/untracked 完整改动范围、1 GiB 稀疏文件有界读取、MCP 截止时间、进程组超时清理、精确 patch 校验、API、CLI、评估器和完整 issue-to-patch 旅程。

## 三个 fixture

`config_validation` 要求缺失端口时使用 8080，并拒绝 TCP 范围外的值，只允许修改 `config.py`。评估器在补丁结果已提交后注入崩溃，重新构造 service 并 resume；最终通过隐藏测试，恢复次数为 1，同一工具副作用未重复执行。

`divide_zero` 要求分母为零时返回 `None`，只允许修改 `calculator.py`。`slug_normalization` 要求把连续空白与标点规整成一个连字符，只允许修改 `slug.py`。每个 case 都创建新 Git 仓库和独立 worktree；Agent 结束后才安装隐藏测试。

| Case | 结果 | 改动范围 | 审计工具事件 | 恢复 | Patch SHA-256 前 12 位 |
|---|---|---|---:|---:|---|
| config_validation | resolved | `config.py` | 7 | 1 | `09ad406c48fd` |
| divide_zero | resolved | `calculator.py` | 6 | 0 | `9cb0b5327afa` |
| slug_normalization | resolved | `slug.py` | 6 | 0 | `6acc019f75ed` |

“审计工具事件”包含 `tool.started`、终结事件和恢复场景的 `tool.replayed`，不是模型思考步数。补丁与改动范围来自生产同款完整差异工具：以 `HEAD` 读取 staged/unstaged tracked 变更，再用 `git ls-files --others --exclude-standard -z` 纳入 untracked 文件；哈希和路径都不读取模型自报。

## 复现命令

```bash
python3.13 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
npm --prefix web ci
sh scripts/verify.sh
```

脚本依次执行 Ruff、mypy、pytest + branch coverage、离线 benchmark、Vitest、TypeScript 类型检查与 Vite 构建。未设置 `REPOPILOT_VERIFY_DIR` 时，它会在 `/tmp` 创建新目录并打印报告路径；指定目录时，目录下不能已有 `evaluation`，从而避免覆盖旧证据。

单独重跑 benchmark：

```bash
.venv/bin/repopilot eval --workspace /tmp/repopilot-eval-new --json
```

JSON 中的 `showcase.workspace` 是可直接交给 `repopilot serve --workspace` 的 case 工作区，里面同时存在任务数据库、worktree 和四份证据文件。这样 API、SSE 和 React 面板展示的是刚刚跑出的真实任务，而不是另一个空数据库。

## 这组数字不能说明什么

三例规模较小，覆盖配置验证、异常分支和文本规整，不代表真实大型仓库解决率，也不是 SWE-bench、SWE-bench Verified 或任何外部排行榜成绩。脚本化 runtime 消除了模型采样波动，因此这里测量的是 Agent 控制面、工具执行、恢复、安全边界和验收链路，不是 GPT-5.6 的代码能力。

下一轮有意义的评估应增加 20 至 50 个多文件 fixture，再接固定版本的公开 benchmark 子集。在线模型运行还需要记录模型快照、推理参数、token、成本、延迟和重复运行置信区间；企业试点则应加入人工接管率、PR 接受率、回滚率和合并后缺陷率。

本仓库只在[项目简介与简历](PROJECT_BRIEF_AND_RESUME.md)中使用上述可核验数字。逐项验证证据见[最终验证报告](VERIFICATION_REPORT.md)，技术选择依据见[调研来源](SOURCES.md)。
