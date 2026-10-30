# MCP4Intervals

读取 Intervals.icu，生成中文骑行日报，支持用户自选模型 API 分析和 Server酱微信推送。
可独立定时运行，也可把工具合并到树莓派上现有的 `futureweb/intervals-mcp-server`，复用其远程传输及 OAuth 权限。

当前为单用户实现。每个独立部署配置自己的 Intervals.icu、模型和微信凭据；共享服务器的多用户模型及接收人隔离暂未实现。

## 已实现功能

| 功能 | 说明 |
|---|---|
| 数据摘要 | 今日睡眠、静息心率、HRV、疲劳，昨日活动、过去7个完整当地日负荷、含今日在内未来7天日历 |
| 历史基线 | 此前42天静息心率均值及有效样本数；HRV 展示数值和有效样本数 |
| 自选模型分析 | 配置 API 地址、模型名、Key；使用 OpenAI 兼容的 Chat Completions 格式，无固定厂商或模型 |
| 日报预览与推送 | 规则摘要或 AI 报告；也可推送外部教练生成的 Markdown 原文 |
| 每日定时 | 默认 Europe/Amsterdam 07:00，支持夏令时、当天计划时间后的启动补发 |
| MCP 接入 | 本地 stdio 推送工具；现有 futureweb 远程 MCP 的摘要、AI 分析及推送工具集成 |
| 部署 | Windows 脚本、独立日报 Docker Compose、现有 MCP 源码集成脚本 |

程序仅提供分析和建议，不自动修改 FTP、课程或训练日历，不读取聊天记录。

## 本地安装

需要 Python 3.11+；合并到现有 futureweb 服务时使用其要求的 Python 3.12+。

```powershell
Set-Location 'F:\git\MCP4Intervals'
powershell -ExecutionPolicy Bypass -File .\scripts\setup-windows.ps1
notepad .env
```

基本配置：

```dotenv
INTERVALS_API_KEY=你的Intervals_API_Key
INTERVALS_ATHLETE_ID=0
SERVERCHAN_SENDKEY=你的SCT开头SendKey
REPORT_TIMEZONE=Europe/Amsterdam
REPORT_TIME=07:00
REPORT_MODE=summary
```

`.env` 只保留在自己的设备上。微信推送使用 Server酱 Turbo 的 `SCT...` Key，未接入 SC3 的 `sctp...`。
配置并绑定接收通道后，正文会经过 Server酱；服务接受请求不代表手机已经收到。

## 配置自选模型

在 `.env` 填写三个字段即可接入支持 Chat Completions 格式的模型：

```dotenv
LLM_BASE_URL=https://你的模型服务地址/v1
LLM_MODEL=你选择的模型名
LLM_API_KEY=你的模型Key
REPORT_MODE=ai
```

`LLM_BASE_URL` 填厂商提供的 API 根地址（不自动补 `/v1`），程序会追加 `/chat/completions`；也支持直接填完整 `/chat/completions` 地址。
这是一种接口格式，不绑定 ChatGPT，也不要求 OpenAI 账号。仅提供原生其他协议的模型服务需通过兼容网关接入。

本地模型示例：`LLM_BASE_URL=http://localhost:11434/v1`，无需认证时 `LLM_API_KEY` 可留空。
Docker 内的 localhost 指容器本身；访问树莓派上的另一容器时使用相应服务名，可信局域网的明文 HTTP 需显式设置 `LLM_ALLOW_HTTP=true`。

可选配置：`LLM_TIMEOUT=120`（秒）、`LLM_MAX_TOKENS=2500`。模型接口接收 `model`、`messages`、`max_tokens` 和 `stream=false`，返回 `choices[0].message.content`。

AI 模式会把归一化的训练摘要（含指标、活动名称及日历名称）发送至配置的模型服务，可能产生该服务的调用费用。
不发送 Intervals API Key、SendKey 或原始活动轨迹。空正文、截断、错误格式和超长输出都会报错，不会悄悄降级成规则摘要推送。

## 命令

所有命令均使用 `python -m mcp4intervals`；Windows 可将 `python` 换为 `.\.venv\Scripts\python.exe`。

```powershell
# 完全离线的规则摘要预览
python -m mcp4intervals preview --fixture examples/snapshot.json --date 2026-10-30 --mode summary

# 实时数据预览，不发微信
python -m mcp4intervals preview --mode summary

# 实时数据交给自选模型分析，不发微信（调用模型 API）
python -m mcp4intervals preview --mode ai

# 用示例数据测试自己的模型接口（也会调用模型 API）
python -m mcp4intervals preview --fixture examples/snapshot.json --date 2026-10-30 --mode ai

# 当天日报生成并推送；默认模式取 REPORT_MODE，可用 --mode 覆盖
python -m mcp4intervals send --mode ai

# 发送不含训练数据的连接测试
python -m mcp4intervals test-push

# 定时进程；模式取 REPORT_MODE
python -m mcp4intervals daemon

# 推送已有报告原文；与远程 MCP 推送工具共用 coach 类型的每日名额
python -m mcp4intervals send-file ./today-report.md
```

`preview` 可查历史日期；`send`、`send-file` 和 MCP 推送只接受配置时区的今天。
fixture 只能用于预览，不能推送为真实日报。

定时进程每分钟检查一次，需持续运行。关闭进程、电脑关机或休眠期间不能发送；仅补发启动当天，不追补往日。
数据拉取失败会重试；一旦模型请求失败，当前进程当天不再自动调用模型，可修复后手动 `send --mode ai`。
模型失败的停用标记仅在内存中，重启会重新尝试；已占用的推送名额则持久化保存。

## 树莓派部署

**合并到现有远程 MCP**：见 [docs/AI_COACH_INTEGRATION.md](docs/AI_COACH_INTEGRATION.md)。工具复用现有服务，不新增远程监听端口。

**独立定时日报**（可选）：配置项目 `.env` 后运行：

```bash
docker compose build
docker compose run --rm wechat-reporter python -m mcp4intervals preview --mode ai
docker compose up -d
docker compose logs --tail 30
```

独立容器不开放端口。命名卷持久化推送状态，`docker compose down -v` 会删除状态。
若由远程教练生成并发送原文，通常无需再开独立日报 daemon，否则会收到 daily 和 coach 两类报告。
Windows 登录启动可使用任务计划程序运行 `scripts/run-daily.ps1`，脚本不会自动创建任务。

## 本地 stdio MCP

```powershell
python -m pip install -r requirements-mcp.txt
python -m mcp4intervals mcp-stdio
```

提供 `send_daily_coach_report(report, report_date)`；stdio 等待本地 MCP 客户端通信，不是远程 URL。
远程摘要和 AI 分析工具通过上述 futureweb 集成提供。

## 数据与发送约束

- 当天恢复数据缺失显示“未知”，不复用昨天；日历无记录不代表休息。
- 近7天负荷排除今天，缺失数量单列；HRV 来源未核实，不据此自动判断恢复或加量。
- SQLite 按接收配置、消息类型、日期原子占位，限制每类每天一次自动推送尝试；状态不存正文。
- 超时或异常后不自动重发；`uncertain` / `sending` 需人工核对微信和控制台，尚无强制重发命令。
- 已占用推送名额会在调用模型前检查，减少重启后的重复开销；并发进程仍可能重复生成 AI 报告，但推送保留原子去重。
- `accepted_by_serverchan` 只表示服务接受，未实现手机送达回执。
- 标题最多80字符、正文最多28000 UTF-8字节；超长拒绝发送，不自动截断。

## 验证

```powershell
python -m unittest discover -s tests -v
```

测试使用合成数据和模拟服务；真实模型账号、真实微信及树莓派部署需在配置后验证。
现有 futureweb 接入的验证版本、权限行为及测试命令见集成文档。
