# ForgePilot 快速开始

ForgePilot 是本地单用户代码任务工作台。浏览器与终端界面连接同一个后台服务，可以提交实现任务、启动只读分析、查看进度、审批具体操作，以及读取测试和补丁证据。`repopilot` 命令和 Python 模块保留兼容。

## 安装并运行离线演示

在项目根目录，用 Python 3.12 或更新版本创建环境并安装：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
cd web
npm ci
npm run build
cd ..
.venv/bin/forgepilot ui --demo --workspace /tmp/forgepilot-demo-001
```

打开服务启动时打印的本地地址，默认是 [http://127.0.0.1:8765](http://127.0.0.1:8765)。页面右上角应显示 `Demo · offline`。此模式不需要模型 Key，使用离线 fixture 执行真实后台任务并产生日志和产物；它不证明真实模型已经连通或能独立完成任务。

在 `Task prompt` 输入一个任务，选择 `Implementation task` 或 `Read-only analysis`，点击启动。空任务列表也能直接提交。后台状态会自动刷新，任务执行完毕后可检查摘要、错误信息，以及 Patch / Test report / Trajectory / PR draft。下方 `Run audit` 保留详细事件与验证证据。

另开终端连接同一个服务：

```bash
.venv/bin/forgepilot tui --url http://127.0.0.1:8765
```

退出浏览器或 TUI 不等于取消任务。取消按钮发出协作式取消请求，外部请求或命令可能需要等到超时或安全边界才停止。审批只绑定显示的 operation ID 和持久化参数，批准后由服务继续任务；中断任务可尝试恢复，服务仍会拒绝不能安全恢复的状态。

## 连接真实模型

先选择现有样例：[DeepSeek](../examples/deepseek.toml)、[Anthropic](../examples/anthropic.toml) 或 [Ollama](../examples/ollama.toml)。把配置复制到**被操作仓库之外**的可信位置，检查 endpoint、模型 ID、`api_key_env`、Skills 路径和 MCP 白名单。模型名称及参数应按供应商当前账户能力填写。配置里只保存环境变量名称，不能保存 Key 本身。

以下隐藏输入例子使用 Bash；macOS 默认 zsh 用户先运行 `bash`，然后在同一个终端会话中执行：

```bash
read -r -s -p 'DEEPSEEK_API_KEY: ' DEEPSEEK_API_KEY
export DEEPSEEK_API_KEY
printf '\n'
```

将变量名换成配置 `api_key_env` 指定的名称。输入不会显示，也不作为命令参数写入 shell 历史。不要把 Key 发到聊天、浏览器表单、任务描述、源码或 Git。程序不会自动加载目标仓库中的 `.env`；Key 只从启动服务的进程环境取得。任务结束、服务停止后可运行 `unset DEEPSEEK_API_KEY` 清除当前 shell 变量。

从已安装 ForgePilot 的项目根目录启动在线工作台：

```bash
.venv/bin/forgepilot ui \
  --repo /absolute/path/to/trusted/repository \
  --config /absolute/path/outside-target/config.toml \
  --test-command '/absolute/path/to/python /absolute/path/outside-target/acceptance.py' \
  --workspace /tmp/forgepilot-online-001
```

页面应显示 `Online · live model`，并显示启动时固定的仓库、测试命令和模型。浏览器不能任意改配置、仓库路径或执行命令，也不会接收模型 Key。服务启动只提供入口；提交在线任务后才会实际调用模型。仓库内容、任务描述、读取的 Skills 和工具结果可能发送到配置的供应商，运行前确认数据接收方及费用预算。

首次真实模型测试建议使用 [LIVE_E2E.md](LIVE_E2E.md) 的独立仓库准备流程，避免直接用日常工作仓库试跑。

## 状态和排错

| 页面情况 | 处理方法 |
| --- | --- |
| `Disconnected` / 连接失败 | 确认本地服务仍运行；使用启动时打印的地址，点击 Reconnect。 |
| `Task controls unavailable` | 当前连接的是旧版审计 API；旧审计仍可用，启动新版 `forgepilot ui` 后刷新。 |
| `WAITING_APPROVAL` | 查看请求参数和 operation ID，再决定是否批准这一操作。不要凭任务描述批准未知命令。 |
| `Cancellation requested` | 等待 worker 到达安全边界；这不是已经停止的确认。 |
| `FAILED` | 查看错误和测试报告；修正任务或环境后创建新任务。 |
| 产物暂不可用 | 任务可能尚未结束；完成后再次点击对应产物标签。预览会脱敏并有长度上限。 |
| 页面未构建 | 在 `web` 目录运行 `npm ci` 和 `npm run build` 后重启服务。 |

后台进程和队列元数据保存在本地主机。SQLite 是默认任务存储；可选 MySQL 的配置、Docker 准备和真实数据库测试边界见 [MYSQL.md](MYSQL.md)。本地工具策略和 worktree 隔离不等于针对恶意仓库代码的操作系统沙箱。
