# 技术调研来源与取舍记录

资料访问日期为 2026-08-24。下列来源用于决定架构和项目定位，不用于替本项目背书。外部仓库的星数、榜单成绩和产品能力没有写进 RepoPilot-R 的评估结果。

## Agent 与模型接口

- [OpenAI latest model guide](https://developers.openai.com/api/docs/guides/latest-model)：确认当前 GPT-5.6 选型、Responses 路径和明确自主边界的提示原则。RepoPilot-R 的可选在线适配器默认模型名为 `gpt-5.6`，核心离线评估不调用它。
- [OpenAI model catalog](https://developers.openai.com/api/docs/models)：核对模型命名和能力入口。
- [OpenAI Agents SDK: running agents](https://openai.github.io/openai-agents-python/running_agents/) 与 [tools](https://openai.github.io/openai-agents-python/tools/)：参考结构化输出、Runner 和工具建模。项目只让 SDK 做决策，工具执行仍由本地网关掌握。
- [learn-claude-code](https://github.com/shareAI-lab/learn-claude-code)：参考渐进式教学项目中的最小循环、工具分发、权限、成对压缩、有界错误、worktree 和 MCP 命名空间。RepoPilot-R 重新实现了领域模型、状态持久化和评估，没有复制其代码。

## 软件工程 Agent 参考

- [mini-SWE-agent](https://github.com/swe-agent/mini-swe-agent)：它把 Agent 控制面压到很小，说明可读 harness 本身有作品价值。RepoPilot-R 保留小循环，同时把可靠性职责拆进状态机、事务网关和评估器。
- [SWE-agent documentation](https://github.com/SWE-agent/SWE-agent/blob/main/docs/index.md)：参考 Issue 到 patch 的任务边界和基准评估语境。
- [OpenHands](https://github.com/All-Hands-AI/OpenHands)：参考 SDK、控制面、沙箱服务和 UI 分层。
- [OpenHands sandbox server](https://github.com/OpenHands/sandbox-server/blob/main/README.md)：用于区分 worktree 隔离与真正执行沙箱。RepoPilot-R 没有声称实现同等级容器隔离。

这些项目帮助回答“一个有分量的软件 Agent 项目应该展示什么”。最终选择是可靠性优先的 RepoPilot-R，而不是再做通用聊天或资料搜索 Agent，因为状态恢复、策略和可复现评估能展示更完整的后端工程栈。

## 协议、安全与可观测性

- [Model Context Protocol repository](https://github.com/modelcontextprotocol/modelcontextprotocol) 与 [MCP Security](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/SECURITY.md)：参考协议的信任边界和漏洞报告入口。
- [MCP security best practices, 2026-07-28](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/docs/docs/2026-07-28/tutorials/security/security_best_practices.mdx)：参考最小权限、用户同意、token audience 和 confused-deputy 风险。项目因此要求工具来源命名空间和显式 allowlist。
- [MCP authorization security considerations, 2026-07-28](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/docs/specification/2026-07-28/basic/authorization/security-considerations.mdx)：用于规划生产 OAuth 接入；当前仓库没有实现完整授权服务器。
- [OpenTelemetry GenAI semantic conventions](https://opentelemetry.io/docs/specs/semconv/registry/attributes/gen-ai/)：该规范提醒输入、工具参数和结果可能包含敏感内容。RepoPilot-R 的 OTel 适配器只导出 ID 与数值指标。

## 基础技术官方文档

- [Git worktree](https://git-scm.com/docs/git-worktree)：任务分支与目录隔离。
- [Python sqlite3](https://docs.python.org/3/library/sqlite3.html)：事务、连接和参数绑定。
- [Pydantic](https://docs.pydantic.dev/latest/)：不可变领域模型与边界验证。
- [FastAPI](https://fastapi.tiangolo.com/)：版本化 REST、验证错误和 SSE 响应。
- [Tree-sitter](https://tree-sitter.github.io/tree-sitter/)：Python 符号提取。
- [React](https://react.dev/) 与 [Vite](https://vite.dev/)：证据仪表盘、类型检查和生产构建。
- [GitHub Actions](https://docs.github.com/actions)：Python 3.12/3.13 与 Node 24 验证矩阵。

## 个人背景与项目定位

项目方向结合了用户提供的[共享对话](https://chatgpt.com/share/6a8aceec-ae74-83ec-9f05-2c2dbcffd9f3)：计算机科学/数据科学背景、Python 与 LLM 技术栈、AI Agent 实习目标和奥克兰求职场景。简历材料因此强调软件工程 Agent、可靠性和可验证指标，不虚构用户人数、收入、SWE-bench 分数或生产客户。

## 证据分界

本项目可声明的数字只有本仓库命令生成的结果，包括测试数、分支覆盖率、三组 fixture、恢复次数和策略违规数。外部来源只解释为什么选择这些技术。评估限制、报告 SHA-256 和复现命令见[评估说明](EVALUATION.md)。
