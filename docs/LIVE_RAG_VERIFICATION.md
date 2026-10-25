# 真实 DeepSeek RAG 验收（2026-10-25）

状态：真实模型与宿主验收通过；不是离线合约模型结果。

## 环境与任务

- Provider：api.deepseek.com，模型 deepseek-flash；用户在本机终端隐藏输入新 Key，Key 未进入任务、源码或本文。
- MySQL 保存真实任务、事件与操作结果；任务/审批队列仍为本地 SQLite。RAG 使用私有 SQLite FTS5 索引。
- 候选仓库基线：`50d07d306c5431415771002b00228336c0c3a721`，独立 Git clone/worktree。
- 任务：修复“有效 HEAD 的空 Git 仓库检索失败，以及删除最后一个可见文件后无法清空索引”。外部 0444 验收位于仓库之外，初始 8 项检查中 3 项失败，未修改验收来放行。

## 真实执行过程与限制

共两次在线任务。第一轮模型只调整 Git 参数顺序，没有修复问题，随后在调试测试中替换超时函数；操作者没有批准执行该调试命令，取消任务并保留失败工件。此前批准过一次 pwd 和三次确切 pytest 命令，没有修改白名单。

第二轮从干净基线开始，提供明确根因诊断：候选列表为空时不要向 check-ignore 传入单个 NUL。模型先添加测试、运行固定验收得到失败，再修改实现；修复后还纠正了自身新增测试里“未删除的未跟踪文件不应出现在 ls-files”的错误断言。最终测试并未削弱预期行为。该结果包含人工诊断引导和审批干预，不能称为一次零干预自主成功。

生产修改只在 `src/repopilot/tools/rag_sources.py`：候选列表为空时忽略集合直接为空，否则原逻辑照常执行。另一修改是 `tests/integration/test_rag.py` 的两项回归测试。Git 根/HEAD、异常退出码、忽略与敏感路径、链接检查、时间/大小限制及固定验收均未放宽。

## 检索证据

模型在修改前后分别发起新的 `rag_search(query="candidates", top_k=5)`，没有重放旧 operation。持久化输出已通过 MySQL 只读查询复核，并将片段逐行与基线/最终源文件核对：

| 阶段 | 引用 | 文件 SHA-256 | 更新文件数 |
| --- | --- | --- | --- |
| 修改前 | `src/repopilot/tools/rag_sources.py:123-161` | `18bef6069d4d1773533bbc29762da24236b4eb0b5b27bec737a5b4ce590f1f49` | 102 |
| 修改后 | `src/repopilot/tools/rag_sources.py:123-167` | `7458cb908c5370dea624b1da16fe7b045f05f31a14916879fbcae32effcafe8f` | 1 |

首次构建索引为 102 个合格文件；修改后只有目标源码文件的可索引内容发生变化。行号、片段正文和完整文件摘要均吻合真实文件。引用不是凭模型最终回答推断。

## 验收证据

- 模型运行固定外部验收：8/8 PASS，候选全量 `397 passed, 5 skipped`。
- 宿主随后独立运行同一命令：8/8 PASS，候选全量 `397 passed, 5 skipped`。
- 任务成功后再次从最终 worktree 独立复验：8/8 PASS，`397 passed, 5 skipped`（68.02 秒）；验收脚本摘要前后相同。
- 合入主项目的完整回归：`397 passed, 5 skipped`，综合覆盖率 88.52%，保持 85% 门槛，warnings 当作错误。
- 5 项 skip 来自未显式启用的 MySQL 专属测试及 SQLite 不适用的行锁用例；实际在线任务使用了真实 MySQL，两者不可混为同一验证口径。
- 全量 pytest 使用 warnings 当作错误。独立验收另检查环境中没有指定模型/数据库凭据变量；不以真实 Key 作为测试数据。
- 接入主项目时，先仅引入两项新增测试：旧实现 2 failed；再引入模型生成的实现：RAG 26 项测试通过。Ruff 与 mypy（53 个源文件）通过。
- 独立只读审查未发现重要问题；该审查没有重跑模型、访问凭据或编辑候选。

成功 job：`6e58d4b5-1fe5-49b4-9dae-271761bf4b6a`；task：`d76d5b78-6304-4e58-8c8e-9013f2e04dce`。

外部验收 SHA-256：`51d4c738c047d3a1e3a7e92f44b56b050f02623a65dc633e881537aaa5ab781a`。

四类工件与 API 返回摘要均一致；原始数据保留在本机，不上传公开仓库：

| 工件 | SHA-256 |
| --- | --- |
| patch | `91c2bea278a015ec5a4024672830333f7366f461d405373c9fea5c2f53ae62d0` |
| test_report | `60f0c8208f5c0b60079a4ebadfc7b82f86f03cf9eb9e894ca7dda7ab56401f08` |
| trajectory | `42f56b857e93a97f9629bae8a3ac32aef2fab9d238b91961164f1b4ad589278e` |
| pr_draft | `0d5bd2c90ce2801ed871618353464eb0465d2b87cfe2dbe42638efa95a1e31f9` |

本次不报告未测量的模型成功率、费用或检索 Recall/MRR。凭据、数据库、索引缓存与原始轨迹不纳入 Git；后续排错见 [DEBUG_JOURNAL.md](DEBUG_JOURNAL.md)。
