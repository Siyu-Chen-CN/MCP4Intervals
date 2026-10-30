# 合并到现有 futureweb 远程 MCP

本集成用于树莓派上现有的 [futureweb/intervals-mcp-server](https://github.com/futureweb/intervals-mcp-server)。
复用其 MCP 实例、网络传输、OAuth、`@tool` 权限和 Intervals.icu API 客户端；不另起远程服务。

## 两种分析方式

1. **客户端自己分析**：任意兼容客户端调用 `get_daily_coach_summary` 获取数据，用它自己的模型分析，再调用 `send_daily_coach_report` 发送原文。无需配置服务端模型 Key。
2. **服务端自选模型**：用户配置 `LLM_BASE_URL`、`LLM_MODEL`、`LLM_API_KEY`，调用 `analyze_daily_training` 返回 AI 日报，再调用发送工具。模型供应商不限，但接口需兼容 Chat Completions。

两种方式都不会修改 FTP 或训练日历。分析和微信发送分成两个工具，调用分析不会自动发微信。

## 新增工具与权限

| 工具 | 参数 | 权限 | 行为 |
|---|---|---|---|
| `get_daily_coach_summary` | `report_date` 可选，默认今天 | read | 使用现有 Intervals 客户端读取数据，返回中文摘要 |
| `analyze_daily_training` | `report_date` 可选，默认今天 | write | 读取数据并发送摘要至服务端配置的模型 API，返回完整 AI 报告 |
| `send_daily_coach_report` | `report`、`report_date` 必填 | write | 通过 Server酱推送完整报告，仅接受今天 |

日期均使用 `REPORT_TIMEZONE`（默认 Europe/Amsterdam）。摘要与分析可指定历史日期。
模型调用可能计费且向外部模型发送训练摘要，因此标为 write 和 `openWorldHint=true`；微信发送同样标为外部写操作。
只读 OAuth 连接看不到、也不能调用两个写工具。需要在服务器启用 write，并给客户端连接授予 write；仅修改服务器配置不会提升原有只读授权。

当前集成仅支持 `MCP_TENANCY=single`；启用 multi 或错误模式值时，在读取数据或发送消息前拒绝执行这些工具。
这只是单用户范围限制，不是已实现多用户隔离。

## 集成源码

本次核对并在本地副本验证的上游版本：`bf98d225d37620e83f67c04ff17c99d7c7d3c51a`（2026-10-30 获取）。
实际部署版本若不同，先比较 `mcp_instance.py`、`tenancy.py` 和 `server.py`。
脚本会检查必要入口和打包格式；不识别的布局会报错，避免猜测修改位置。

把两个仓库放在树莓派或构建机的相邻目录，例如 `~/src/MCP4Intervals` 和 `~/src/intervals-mcp-server`。
**在现有服务的源码副本中集成和构建**，保留运行中服务的配置、OAuth 状态卷和镜像，便于回退。

```bash
cd ~/src/MCP4Intervals
python3 scripts/integrate-futureweb.py ../intervals-mcp-server

cd ../intervals-mcp-server
uv lock
uv sync --all-extras
uv run --locked ruff check .
uv run --locked mypy src tests
uv run --locked pytest
uv run --locked pytest ../MCP4Intervals/integrations/futureweb/test_coach_delivery.py
```

需要 Python 3.12+ 和 uv，沿用上游开发环境要求。
脚本复制 `mcp4intervals/*.py` 到上游 `src/mcp4intervals`，在工具注册后、`mcp.mark_instructions()` 前加入一次适配器导入，并把该包加入 wheel 和 sdist 的打包清单。
不复制 `.env`、API Key、状态库或日志；可重复执行更新源码。新增包和改动应一起纳入自己的部署源码版本控制。

原有 Dockerfile 先构建安装 wheel，再只复制虚拟环境，因此修改打包清单是必需步骤。脚本已经处理，不要只把 Python 文件挂载到源码目录。

## 在现有服务配置中追加

保留原有 `API_KEY`、`ATHLETE_ID`、OAuth、网络地址、HTTPS 和状态文件配置。
远程工具读取训练数据使用上游 `API_KEY` / `ATHLETE_ID`，无需重复填写本项目独立日报用的 `INTERVALS_API_KEY`。

```dotenv
MCP_TENANCY=single
MCP_TOOLSET=full
MCP_PERMISSIONS=read,write

REPORT_TIMEZONE=Europe/Amsterdam
SERVERCHAN_SENDKEY=你的SCT开头SendKey
SERVERCHAN_CHANNEL=
COACH_STATE_DIR=/data/coach-delivery

# 仅使用服务端 AI 分析时填写；客户端自己分析可以全部留空
LLM_BASE_URL=https://你的模型服务地址/v1
LLM_MODEL=你的模型名
LLM_API_KEY=你的模型Key
LLM_TIMEOUT=120
LLM_MAX_TOKENS=2500
LLM_ALLOW_HTTP=false
```

`MCP_TOOLSET=core` 不包含新增工具，需使用 `full`。
`COACH_STATE_DIR` 必须位于当前 MCP 进程可写的持久化卷内。未指定时取 `OAUTH_STATE_FILE` 所在目录下的 `coach-delivery`，默认 `/data/coach-delivery`。
上游 Docker 用户为 UID/GID 10001；沿用其可写的 `/data` 卷即可，绑定目录需有相应权限。
如果旧服务曾使用本地 stdio/文件推送，共用同一份 SQLite 状态目录才会跨入口去重；不同目录不会共享发送名额。

按现有部署方式重新构建并更新该服务镜像；继续使用原有命令、端口、OAuth 配置和卷。不要将独立日报容器替换成 MCP 服务。
本仓库的 `compose.yaml` 是可选的独立定时日报，不是现有远程 MCP 的替代部署。

## 验证调用

重启集成后的服务，重新连接或刷新 MCP 客户端的工具列表，确认三个新工具存在。

先调用摘要工具，不触发模型或微信：

```json
{"report_date": "2026-10-30"}
```

需要服务端模型分析时，将同样参数传给 `analyze_daily_training`。报告日期可省略，默认当前本地日。
成功后，将实际报告原文传给发送工具，日期必须为调用当天：

```json
{
  "report_date": "2026-10-30",
  "report": "这里放实际生成的完整中文报告。"
}
```

`accepted_by_serverchan` 表示服务接受；`already_sent` 表示已有同日 coach 消息，不再发送。
失败或不确定时保留返回的报告，并在微信和控制台核对；不能因为模型重写了文字就绕过同日发送限制。

## 定时运行

现有远程 MCP 本身不因注册工具而自动每天调用模型。
- 外部教练/客户端负责定时：按“读取/分析 → 发送原文”流程调用工具，无需另开日报 daemon。
- 本仓库负责定时：独立日报 daemon 设置 `REPORT_MODE=ai` 并配置 `INTERVALS_API_KEY`，按 `REPORT_TIME` 生成和推送。

选择其中一条发送路线即可；两条同时启用时，daily 和 coach 是不同消息类型，可能各发送一条。

## 验证范围

本次本地已验证：归一化模型请求、模拟 HTTP 模型响应、规则/AI CLI 流程、重复发送检查、MCP 协议内存连接、只读/写权限和单用户范围限制。
上游工具目录及权限测试在 Windows 上可运行。完整上游 OAuth/多用户测试涉及 Linux 的 `fcntl`、`os.fchmod` 等接口，不能把 Windows 下的失败算作树莓派验证通过；应在树莓派 Linux 构建环境运行完整检查。
未使用真实模型 Key、真实微信通道或树莓派连接；集成脚本修改的是源码，不会 SSH 登录或重启正在运行的服务。
