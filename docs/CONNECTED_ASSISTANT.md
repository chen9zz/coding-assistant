# 连接真实模型、MCP 与 Skills（0.2）

## 名称和本次检查结论

Repo = repository，Pilot = 辅助驾驶代码仓库，R 对应原设计的 Reliable / Reliability（可靠性）。最早设计标题是 `RepoPilot-R: Reliable Software Engineering Agent`，不是 R 语言版本。目前代码地图的语法级索引针对 Python；文本读取、补丁和命令工具可用于其他语言，但不能称为已适配 R 的语义索引或 R 专用执行环境。

此前版本只有可靠执行底座：CLI 必须输入预写 actions，MCP 需要调用方注入会话，Skills 没有接入。原有 133 项测试和 3/3 离线演示并不能证明模型自主编程能力。本次新增了真实通信与可用入口。

| 能力 | 现在实现 | 边界 |
| --- | --- | --- |
| 模型驱动编程 | 原生 tool calls → 读文件/搜索/改补丁/运行测试 → 反馈 → 下一步 | CLI 任务式，类似 exec 工作流，不是完整交互式 TUI |
| MCP | 官方 Python SDK 2；stdio、Streamable HTTP；初始化、分页发现、调用、关闭 | 显式工具白名单；支持 Bearer 环境变量；没有 OAuth/旧 SSE |
| Skills | `*/SKILL.md` YAML 元数据；正文及引用按需加载 | 无自动脚本执行、无权限提升；目录须显式配置 |
| 其他模型 API | OpenAI-compatible Chat Completions；Anthropic Messages | 模型须支持工具调用；不代表所有厂商/模型模式均实测 |
| 审批/恢复 | SQLite 检查点，精确请求审批后继续在线推理 | 不是跨任意配置、任意远端状态的事务恢复 |
| 结果 | 独立 Git worktree，真实测试验收，自动输出四件套 | 必须产生非空 patch；纯问答/只读审查不是此任务工作流 |

## 安装和首次运行

在项目根目录中使用 Python 3.12 或更高版本：

```bash
python3.13 -m venv .venv
.venv/bin/python -m pip install -e ".[dev,mcp]"
.venv/bin/repopilot doctor
```

原生 HTTP 模型连接不依赖 `openai-agents`；`online` extra 是旧 SDK 适配器选项。只有使用 MCP 时才需要 `mcp` extra，开发测试依赖中也包含它。

1. 选择 `examples/deepseek.toml`、`examples/anthropic.toml` 或 `examples/ollama.toml`。修改模型 ID、地址和可选技能目录。配置只保存环境变量名，不保存密钥。
2. 在本机终端设置配置指定的环境变量，例如 `DEEPSEEK_API_KEY`。不要把密钥放进聊天、Issue、命令行参数或 Git。
3. 对一个已有提交的可信 Git 仓库运行：

```bash
.venv/bin/repopilot check-config --config examples/deepseek.toml
.venv/bin/repopilot run \
  --repo /absolute/path/to/your/repository \
  --issue "修复端口配置验证，并补充覆盖非法输入的测试" \
  --config examples/deepseek.toml \
  --workspace /tmp/repopilot-online-001 \
  --test-command "/absolute/path/to/project/.venv/bin/python -m pytest -q" \
  --allowed-path "src/*" --allowed-path "tests/*" \
  --max-steps 40 --timeout-seconds 900 --json
```

`check-config` 会启动配置中的 MCP 程序并列出工具，但不调用模型，不证明 API Key 有效。`run` 才会真正向配置地址发请求并产生费用。仓库代码、任务描述、读取的 Skills 和工具结果可能被发送给该地址，请只使用你信任且有权接收这些数据的服务。

源仓库不被直接修改：worktree 基于 HEAD，源仓库未提交改动不会自动复制。使用绝对测试解释器路径可避免隔离工作目录中找不到原来的虚拟环境。仓库自身必须有可运行的测试环境。宿主实际执行测试，不采信模型的“测试已通过”。

成功后证据位于 `<workspace>/artifacts/<task_id>/`：`change.patch`、`test-report.json`、`trajectory.jsonl`、`pull-request.md`。`--json` 输出任务 ID、状态、worktree 等。失败退出码为 1；用 `inspect TASK_ID --workspace ... --json` 查看原因。不会自动推送或提交 PR。

## API 协议与配置规则

`provider.protocol="openai"` 表示 OpenAI-compatible 协议，不限定供应商。`base_url` 是 API 前缀，程序追加 `/chat/completions`。Anthropic 模式追加 `/messages`。本地 loopback 可 HTTP，其他地址必须 HTTPS；不接受带密钥/query 的地址、不跟随重定向。密钥按 `api_key_env` 读取。

可选 `[provider.request_options]` 仅接受受限参数：`max_tokens`、`max_completion_tokens`、`temperature`、`top_p`、`reasoning_effort` 和 `thinking={type="disabled"}`。参数仍须由所选厂商支持；禁止覆盖 messages/tools/model/stream。默认非流式、单工具顺序执行，不支持多模态、thinking/reasoning_content 历史回传和供应商私有服务器工具。DeepSeek 示例显式关闭 thinking；其他兼容服务也需要选择不要求额外推理历史字段的模式。

模型 ID 是配置而不是代码常量。DeepSeek 示例名称依据 2026-10-22 官方文档；以后改模型只需要改配置。所有外部付费厂商调用都仍需自己的有效密钥和一次真实验收。当前已完成的是本地真实 HTTP 协议集成测试，不是对这些账户和服务可用性的保证。

## MCP：先信任服务器，再列出允许调用的工具

stdio 示例在 `examples/deepseek.toml` 的注释中。`command`/`args` 会实际启动进程，只能指向你已检查的程序；不会自动下载或安装服务器。`env` 是「子进程变量名 → 宿主环境变量名」映射，不是密钥字面值。子进程仅获得 SDK 最小系统环境和显式映射变量。

远程 Streamable HTTP 示例：

```toml
[[mcp_servers]]
name = "docs"
transport = "http"
url = "https://your-trusted-server.example/mcp"
bearer_token_env = "DOCS_MCP_TOKEN"
allow_tools = ["search_docs", "get_document"]
timeout_seconds = 20
```

`allow_tools` 使用服务器报告的原始名字，大小写敏感。空列表表示不开放任何工具。白名单工具经命名空间映射、JSON Schema 暴露和网关审计；没有列入的工具既不提供给模型，也不允许调用。工具名带点号/大小写时使用稳定摘要避免冲突；发送给服务器时保留原名。

MCP 服务是可信外部执行边界，不受本地 worktree 文件约束。允许某个远程工具意味着接受其真实权限和副作用，尤其不要随意允许删除、发送消息、支付、发布等工具。超时会尝试取消协议请求，但不能保证远端已发生的副作用回滚；对结局不明的操作先人工核对，不要盲目重跑。

## Skills：元数据 → 正文 → 引用

创建目录结构：`my-skills/regression-repair/SKILL.md`，正文示例：

```markdown
---
name: regression-repair
description: 修复回归缺陷时使用，先构造失败测试，再修改实现并验证。
---
1. 读取相关代码和项目说明，定位可复现失败。
2. 添加能暴露缺陷的测试，然后做最小修复。
3. 使用宿主指定的测试命令验证，说明结果和剩余风险。
```

配置 `skills.roots=["/absolute/path/to/my-skills"]`。相对目录按配置文件所在位置解释，不按当前 shell 位置解释。模型启动时仅收到 name/description；决定采用后调用 `skill_read(name="regression-repair")`，读取引用时增加 `resource="references/guide.md"`。不会把整个技能库塞进系统提示词。

最多 64 个技能，每个资源最多 65536 字节。工具每页返回最多 8000 字符；若 `next_offset` 非空，继续传入该 `offset`，直到读完。名称须与目录一致，重复名称、越界路径、越界符号链接及非普通文本文件会拒绝。`allowed-tools` 只作为技能文档内容，不改变宿主权限。脚本可被阅读，但不会因为出现在 Skill 中就自动执行。

任务历史默认上限 128000 字符，可通过 `--context-chars` 调整（24000–1000000）。这不是 token 数，也不含额外系统提示和工具 Schema，需根据模型窗口及费用设置。旧工具结果按成对历史压缩；超大单个请求仍会安全失败。MCP 结果同时保留文本和结构化字段，超过 12000 字符时返回显式截断的预览，模型应改用更窄查询或远端分页。

## 审批与恢复

当未列入白名单的安全命令需要执行时，任务进入 `WAITING_APPROVAL`。从 `inspect` 输出中的 `task.approval_requested` 找到 operation_id，检查参数后运行：

```bash
.venv/bin/repopilot approve TASK_ID OPERATION_ID --workspace /tmp/repopilot-online-001
.venv/bin/repopilot resume TASK_ID --config examples/deepseek.toml \
  --workspace /tmp/repopilot-online-001 --json
```

批准只绑定这一请求及参数摘要，不会放开所有命令。恢复沿用相同配置、技能、MCP 服务和工作目录。原任务总时间预算包含等待人工审批的时间，因此较长任务请预先设置足够的 `--timeout-seconds`（最高 3600）。当前 API/React 页面仍偏任务审计与离线动作入口，在线连接入口是 CLI，不应宣传成已完成模型配置 UI。

## 代码入口与官方依据

- `agent/providers.py`：真实 HTTP、原生工具调用、响应验证、稳定操作 ID。
- `infrastructure/mcp_transport.py`：单异步会话所有权、连接发现、调用与清理。
- `tools/skills.py` / `tools/catalog.py`：技能按需加载与真实工具 Schema。
- `connected.py` / `configuration.py`：显式配置、连接生命周期、网关注入。
- [Agent Skills 格式规范](https://agentskills.io/specification)
- [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)
- [DeepSeek API](https://api-docs.deepseek.com/) 与 [Tool Calls](https://api-docs.deepseek.com/guides/tool_calls/)
- [Ollama OpenAI 兼容接口](https://docs.ollama.com/api/openai-compatibility)

这些接口测试证明执行链路可工作，不证明某个模型的真实修复成功率。简历中可以写实现了多模型/MCP/Skills 执行框架，但模型自主修复率、延迟和费用必须用后续真实任务集测量，不能用离线 fixture 数据替代。
