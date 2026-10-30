"""Optional personal stdio MCP adapter. Never opens a network listener."""
from datetime import date

from .core import AppError, Config, deliver


def send_coach_report(cfg: Config, report: str, report_date: str) -> dict:
    try:
        day = date.fromisoformat(report_date)
    except ValueError:
        raise AppError("report_date 应为 YYYY-MM-DD。") from None
    if day != cfg.today():
        raise AppError(f"只能发送 {cfg.timezone} 当地今天的教练报告。")
    return deliver(cfg, day, f"每日骑行教练 {report_date}", report, kind="coach")


def run(cfg: Config):
    try:
        # Use the v1 SDK API explicitly; requirements-mcp.txt constrains major version.
        from mcp.server.fastmcp import FastMCP
    except ImportError:
        raise AppError("请先安装 requirements-mcp.txt；该适配器使用 MCP Python SDK v1。") from None

    mcp = FastMCP("MCP4Intervals WeChat")

    @mcp.tool()
    def send_daily_coach_report(report: str, report_date: str) -> dict:
        """External write: send the complete Chinese coach report to the owner's
        configured WeChat via ServerChan. Only use after the owner authorized
        report delivery. report_date is YYYY-MM-DD in Europe/Amsterdam.
        No recipient or secret is accepted as a tool argument. One report/day.
        Never claim handset delivery merely because the provider accepted it.
        """
        try:
            return send_coach_report(cfg, report, report_date)
        except AppError as exc:
            return {"status": "error", "message": str(exc)}

    mcp.run(transport="stdio")
