# 0.2 连接式助手验证报告

日期：2026-10-22。环境：macOS arm64、Python 3.13.13、MCP SDK 2.0.0、PyYAML 6.0.3。在已有隔离工作树中执行 `sh scripts/verify.sh`，退出码为 0。此次复用已安装依赖，不宣称全新机器安装验收；没有调用外部付费模型。

| 检查 | 结果 |
| --- | --- |
| Ruff | All checks passed |
| 严格 mypy | 42 个源文件无问题 |
| pytest（ResourceWarning 作为错误） | 224 passed，49.44 秒 |
| coverage.py，branch=true | 86.76%，门槛 85%（语句与分支合计，不是单独分支命中率） |
| 离线 benchmark | 3/3，1 次故障恢复，0 策略违规 |
| Vitest | 2 passed |
| TypeScript | 无类型错误 |
| Vite | 生产构建通过 |

## 本次新增证据

- `tests/unit/test_providers.py`：46 个本地 HTTP 协议用例，OpenAI-compatible 与 Anthropic 真实请求/响应、凭证环境变量、工具参数、历史证据重建、别名、错误脱敏、禁重定向、输出大小和超时、受限 request_options。
- `tests/integration/test_mcp_transport.py`：真实官方 SDK 子进程及本地 Streamable HTTP 服务；初始化、发现、重复会话调用、大小写/点号工具名、白名单、环境过滤、Bearer、取消、进程回收和不安全地址拒绝。
- `tests/unit/test_skills.py` 与 `test_connected_evidence.py`：技能元数据、正文及引用、重复/非法名称、越界和符号链接、大小限制、30 KB 正文分页可完整还原；MCP structured_content 不丢失，过大结果显式截断。
- `tests/integration/test_connected_cli.py`：本地 HTTP 返回原生模型动作，经真实 CLI → Skills/MCP → 权限网关 → Git patch → pytest → 证据包；另测暂停审批后在线恢复，以及 30 KB 仓库文件读取。
- `repopilot check-config --config examples/deepseek.toml`：成功加载示例并列出七个内置工具，不连接模型；不能把该结果当作 Key 或远端模型可用性验证。

## 独立审查后的修正

审查未发现 Critical 问题，指出三处 Important 问题：大技能输出超过原上下文预算、MCP 丢失结构化结果、CLI 成功后未自动落盘证据包。均先观察回归测试失败，再修复并通过上述全量验证。

大技能现在每页最多 8000 字符；任务历史预算可配置，默认 128000 字符。MCP 同时保留 content 和 structured_content，超过 12000 字符则返回带标记的预览。CLI run/resume 成功时生成四件套。运行时仍保留拒绝越权、审批暂停及宿主最终测试，不以放宽权限换取通过。

## 不能由这些结果证明的事

本地模型 HTTP 服务器是协议测试替身，其动作有确定预期，不是独立大模型推理。MCP 测试使用真实进程与 SDK，但不是所有商业 MCP 服务。没有外部 API Key 的付费实测，没有 SWE-bench 或真实任务修复率。没有完整 CC/Codex 交互式终端/IDE、多模态、后台子代理、OAuth 或 reasoning/thinking 历史回传。

远端取消不等于事务回滚；stdio MCP 和测试进程不是抵御恶意代码的 OS 沙箱。urllib DNS/响应头等待仍不保证硬墙钟取消。本地文件路径校验也不宣称能抵御同权限恶意进程并发替换目录。以上是明确保留的边界，而不是测试通过后可以省略的风险。
