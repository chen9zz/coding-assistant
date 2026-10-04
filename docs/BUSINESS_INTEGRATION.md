# RepoPilot-R 业务对接与落地方案

## 产品位置

RepoPilot-R 适合放在研发流程的“候选修复生成”环节。它接收已有 Issue 和仓库快照，产出可审查 patch、测试证据、事件轨迹与 PR 草稿。它不替代代码所有者，也不直接拥有生产发布权限。最自然的首个客户是有稳定测试、Issue 数量较多、重复维护工作占比高的 20 至 200 人软件团队。

优先场景有三类。第一类是依赖升级、配置校验、API 迁移和小范围缺陷，这些任务边界清楚，容易写确定性测试。第二类是夜间 CI 失败分流，Agent 先复现并给出候选 patch，值班工程师早上审核。第三类是内部平台团队的批量维护，例如统一弃用接口或补日志字段。没有测试、需求含糊或涉及数据库生产迁移的 Issue 不应进入无人值守路径。

## 对接流程

```text
GitHub/GitLab webhook or Jira issue
              |
      intake + repository policy
              |
      RepoPilot-R task API
              |
 isolated runner + evidence store
              |
 approval service / code owners
              |
 draft PR -> CI -> human merge
```

入口服务把 webhook 规范成 `TaskSpec`：仓库镜像路径、Issue、允许的测试参数数组、自然语言建议、宿主强制的路径/文件数范围、步数与总墙钟。`POST /v1/tasks` 创建任务；执行器通过 service 层选择脚本或在线决策 runtime；`GET /v1/tasks/{id}/events` 的 SSE 流推送状态。任务成功后，`GET /v1/tasks/{id}/artifacts` 返回 patch、测试报告、轨迹和 PR 草稿元数据。

当前公开 API 的 `resume` 请求使用结构化 actions，适合可复现演示。生产接入要在受信任 worker 内选择在线 runtime，不应让外部调用方提交任意模型动作。worker 仍调用相同 `TaskApplicationService`，因此策略、幂等和终态验收不会分叉。

## GitHub 对接样例

1. GitHub App 只申请读取代码、读取 Issue、创建分支和写 PR 草稿所需的最小权限；安装范围由组织管理员指定。
2. webhook 收到 `issues:labeled`，只有带 `agent-candidate` 标签且命中仓库 allowlist 才创建任务。
3. 镜像服务用短期 token 拉取固定 commit，随后撤销网络权限。
4. runner 在容器或微虚机中创建 worktree。测试命令来自仓库策略文件，Issue 文本不能覆盖它。
5. `WAITING_APPROVAL` 事件送到代码所有者；审批记录带用户身份、时间和参数摘要。
6. 成功后发布服务校验工件 SHA-256，再创建 draft PR。PR 描述链接到测试报告和只读轨迹查看器。
7. 现有分支保护、CODEOWNERS 和 CI 继续生效，人负责最终 merge。

最后一跳单独做成发布服务很关键。RepoPilot-R worker 不需要长期 GitHub 写 token，模型执行环境也接触不到 PR 创建凭证。

## 企业系统映射

| 现有系统 | 对接点 | RepoPilot-R 输出 |
|---|---|---|
| Jira / Linear | Issue webhook、状态回写 | 任务 ID、状态、失败原因 |
| GitHub / GitLab | 固定 commit、draft PR | patch、改动路径、PR 文案 |
| CI | 允许测试命令、二次流水线 | 测试操作 ID、日志摘要、退出码 |
| Slack / Teams | 审批与告警 | 参数摘要、风险原因、审批链接 |
| S3 / GCS | 不可变工件 | patch、JSON 报告、JSONL 轨迹 |
| OpenTelemetry | 运行指标 | task/operation ID、耗时、计数 |
| SIEM | 安全事件 | 拒绝、审批、重放、失败事件 |

MCP 适合接只读知识与工单工具，例如内部文档搜索或 Jira 查询。每个连接要有服务器命名空间、独立凭证和工具 allowlist。修改工单、触发部署、发送消息这类外部副作用需要远端也支持幂等键，并经过人工审批。

## 试点计划

第 1 至 2 周只离线回放历史 Issue。选择 30 个已有修复和测试的任务，隐藏原提交，让 Agent 生成候选 patch；比较测试通过、改动范围和人工评分。此阶段不写远程仓库。

第 3 至 4 周接一个低风险仓库，系统只创建 draft PR。限制为单文件或明确目录、15 分钟执行、固定测试命令、禁网。每个结果由原维护者评分并记录接管原因。

第 5 至 8 周扩大到三至五个仓库，增加容器隔离、集中审批、成本与 OTel 指标。达到门槛后再允许自动创建 draft PR；自动 merge 不在首期范围。

建议门槛包括：候选任务隐藏测试通过率至少 60%，改动范围合规率 100%，策略绕过 0，人工接管原因可归类，合并后 14 天内回滚率不高于团队同类人工 PR 基线。这里的 60% 是试点准入目标，不是当前项目成绩。

## 商业指标

业务负责人关心每个被接受 PR 节省的工程师时间、从 Issue 到首个可审查 patch 的中位时长、候选 PR 接受率和单位任务成本。平台负责人还要看队列等待、执行失败、审批时长和 runner 利用率。安全负责人关注策略拒绝、越权尝试、敏感信息脱敏命中和异常 MCP 调用。

成本模型可以拆成模型 token、runner 分钟、存储和人工审核分钟。预算器已有步数、时间和输出上限，接入在线模型后再加入 token 与美元上限。低价值任务若预计成本高于人工处理，就不进入 Agent 队列。

## 数据与合规

源码和 Issue 可能包含客户信息。企业部署应按仓库分区保存 SQLite/工件，或把状态迁到 Postgres、工件迁到带 KMS 的对象存储。轨迹设置 30 至 90 天保留，patch 按代码审计策略保留。模型提供商必须满足组织的数据使用约定；敏感仓库可使用私有端点或本地模型。

系统要记录谁创建任务、谁审批、用哪个 commit 和策略版本、最终工件哈希。删除请求不能只删 UI 记录，还要覆盖对象存储、数据库、日志索引和模型缓存。当前本地版本没有多租户或合规认证，这些是企业化工作，不应写成已交付能力。

项目如何转成面试与简历语言，见[项目简介与简历](PROJECT_BRIEF_AND_RESUME.md)。安全控制与未覆盖项见[安全说明](SECURITY.md)。
