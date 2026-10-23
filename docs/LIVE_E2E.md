# 真实模型 E2E：先准备，再由本机启动

此流程验证“真实模型阅读仓库 → 修改代码 → 运行独立验收 → 留下补丁与证据”。准备脚本只使用本地 Git 和 Python，不读取模型 Key、不连接模型、不安装依赖，也不自动开始付费运行。

## 固定任务与验收契约

任务是在本项目提交 `be2a6e58e961c2b2945f564b85cdd52d1667cd7a` 的独立副本中，为 `read_file` 增加字节分页。该原始版本只接受 `path`，会忽略传入的分页参数，因此能够构造可解释的失败基线。

| 行为 | 验收要求 |
| --- | --- |
| `offset` | 零基字节偏移，默认 0，必须是非负整数；bool 不是合法整数。 |
| `limit` | 正整数字节数，默认 64,000，上限 64,000；bool、字符串、小数、零、负数和超限值均返回 FAILED。 |
| 指定页 | 对 `alpha\nbeta\ngamma\n`，`offset=6, limit=5` 返回 `beta\n`。 |
| EOF | offset 到达或超过 EOF 时成功返回空 content。 |
| 元数据 | `size_bytes` 仍表示整文件长度；`truncated` 表示本次读完后还有剩余字节。 |
| 兼容性与安全 | 省略参数仍保持旧读取行为；路径越界、二进制读取仍拒绝。UTF-8 截断沿用原有 replacement 解码规则。 |
| 模型工具 Schema | `tool_catalog` 声明可选 integer offset / limit 及 minimum / maximum，不能只有执行器实现分页。 |

验收脚本不依赖模型添加的测试断言。它位于目标仓库之外，设为只读权限，验收时从当前 worktree 的 `src` 导入候选代码，避免误测安装在宿主环境中的另一份实现。

## 1. 只准备本地副本

先按 [QUICKSTART.md](QUICKSTART.md) 安装开发依赖。在 ForgePilot 项目根目录执行，目标目录必须不存在且位于源仓库之外：

```bash
.venv/bin/python scripts/prepare_live_e2e.py \
  --destination /tmp/forgepilot-live-e2e-001
```

脚本固定检出上述完整提交，不复制当前工作目录里的未提交修改，不覆盖已有目录。可用 `--source /absolute/path/to/local/repopilot-r` 明确选择含该提交的**本地**源仓库；不能改成远程 URL 或任意 baseline。

离线交付的源码目录可以没有 `.git`。此时脚本会自动读取随包附带的 `benchmarks/live-e2e-baseline.bundle`，从这个本地 Git bundle 创建独立副本，并再次验证、检出上述固定提交；即使 bundle 的 HEAD 更新，也不会改变验收基线。保留 bundle 文件即可按同一命令运行，不需要访问远程仓库。若 `.git` 和 bundle 都缺失，脚本会明确报错，不会自动联网下载。目标目录在两种模式下都必须位于源目录之外。

生成结构：

```text
forgepilot-live-e2e-001/
  repository/        独立 Git clone，固定 baseline
  acceptance.py      仓库外、0444 只读的宿主验收脚本
  task.md            可直接粘贴到任务输入框的准确需求
  baseline.json      已实际运行的原始失败基线
  manifest.json      完整提交、验收脚本 SHA-256、命令及路径
```

准备成功的退出码是 0，但 `baseline.json` 应显示 `FAIL`：当前基线有 19 项检查，其中旧调用、路径边界、二进制拒绝 3 项通过，分页及参数/Schema 的 16 项失败。`offset-limit` 和 `catalog-pagination` 必须失败。环境导入错误会显示 `ENVIRONMENT_ERROR` 并使准备失败，不能当作“缺陷已复现”。

脚本记录 `model_calls: 0`。如果准备中断，已生成文件会保留供检查，不会递归删除；修正依赖后使用另一个新目录重试。

基线和外部验收检查的是候选副本，不属于当前 ForgePilot 源码的测试覆盖率。准备脚本会使用精简环境；集成测试重跑候选验收时也会去掉 `COV_CORE_*` / `COVERAGE_*`，避免旧提交污染当前项目的覆盖率结果。

## 2. 在本机配置并启动服务

复制并调整 [examples/deepseek.toml](../examples/deepseek.toml) 或其他供应商样例，保存到目标 `repository/` 之外。用 [快速开始中的隐藏输入](QUICKSTART.md#连接真实模型) 在终端设置配置指定的环境变量。不要将 Key 写入任务、浏览器、源码、命令参数或 `.env`；目标仓库的 `.env` 不会被自动加载。

查看 `manifest.json` 中的 `repository`、`workspace` 和 `test_command_shell`，使用它们的**完整绝对路径**。尤其保留原虚拟环境解释器路径，不要把它替换成当前工作目录里的 `python`，也不要解析其符号链接到系统 Python。

```bash
.venv/bin/forgepilot ui \
  --repo /tmp/forgepilot-live-e2e-001/repository \
  --config /absolute/path/outside-target/config.toml \
  --test-command '/absolute/path/to/forgepilot/.venv/bin/python /tmp/forgepilot-live-e2e-001/acceptance.py' \
  --workspace /tmp/forgepilot-live-e2e-001/workspace
```

以上解释器路径是占位示例，实际应原样使用 manifest 的 `test_command_shell`；路径包含空格时，保留其内部 shell 引号。配置路径必须由你显式选择，不会从目标仓库中自动发现。

打开 [本地工作台](http://127.0.0.1:8765)，确认显示 `Online · live model` 及正确仓库和验收命令。将 `task.md` 全文粘贴到 Task prompt，选择 Implementation task 后启动。这一步才会把任务及后续读取的仓库内容发给配置的模型服务，并可能产生费用。

也可另开终端连接同一服务：

```bash
.venv/bin/forgepilot tui --url http://127.0.0.1:8765
```

## 3. 判断是否真实完成

不能用“界面显示成功”或“模型说已通过”单独判定 E2E 成功。至少保留并检查：

1. 后台任务达到 SUCCEEDED，且工作区有非空补丁，修改限于分页实现、工具 Schema 和相关测试。
2. 宿主独立执行固定外部验收命令，Test report 中退出码为 0，验收内容显示 PASS 且 failures 为空。
3. 在最终 worktree 根目录再次运行 manifest 中记录的同一命令，得到同样的 PASS。不能在未修改的 `repository/` 基线根目录重跑后误判。
4. 外部 `acceptance.py` 的 SHA-256 与 manifest 中的 `acceptance_sha256` 相同；检查 patch、trajectory 和 PR draft，确认没有修改验收机制或绕过要求。

macOS 可用 `shasum -a 256 /tmp/forgepilot-live-e2e-001/acceptance.py` 核对摘要；Linux 可用 `sha256sum`。不要为了让任务通过而修改外部验收文件。

若任务 FAILED，保留错误、trajectory 和测试报告。若 WAITING_APPROVAL，核对精确 operation ID 与参数后再决定批准；不要批准修改验收脚本或扩大命令权限的请求。取消是协作式请求，需要观察最终 CANCELLED 状态。

此处的“外部只读验收”指文件位于 agent 的仓库文件工具范围之外，并设置只读权限；它不是独立用户/容器提供的强制安全边界。以同一系统用户运行的恶意 Python 仍可能绕过普通文件权限，因此只在可信仓库上使用，并用事前摘要核对验收完整性。

完成准备和离线基线，仅能证明真实模型 E2E **已可运行**。只有本机配置有效 Key、实际提交在线任务并取得独立验收 PASS 后，才能报告该模型/配置的真实端到端测试通过。此流程不会自动推送 Git 或创建远程 PR。
