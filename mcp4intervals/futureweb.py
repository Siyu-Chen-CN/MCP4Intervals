"""Tools registered on the existing futureweb server and its OAuth permission guard.

Import this module from intervals_mcp_server.server; do not start another listener.
"""
import asyncio
import json
import os
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from typing import Annotated

from mcp.types import ToolAnnotations
from pydantic import Field

from intervals_mcp_server.api.client import make_intervals_request, seg
from intervals_mcp_server.config import get_config
from intervals_mcp_server.mcp_instance import tool
from intervals_mcp_server.tenancy import canonical_athlete_id, multi_user

from .ai import ModelConfig, analyze_snapshot
from .core import AppError, Config, render_report
from .mcp_stdio import send_coach_report


ReportDate = Annotated[str, Field(description="Report date YYYY-MM-DD in REPORT_TIMEZONE; empty means today")]
EXTERNAL_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True)


def owner_config() -> Config:
    if multi_user():
        raise AppError("教练分析及微信推送目前仅支持 MCP_TENANCY=single；尚未实现多用户模型与接收人隔离。")
    cfg = Config.from_env()
    state = os.getenv("COACH_STATE_DIR")
    if not state:
        state = str(Path(os.getenv("OAUTH_STATE_FILE", "/data/oauth_state.json")).parent / "coach-delivery")
    return replace(cfg, state_dir=Path(state))


def report_day(cfg: Config, value: str) -> date:
    try:
        return date.fromisoformat(value) if value else cfg.today()
    except ValueError:
        raise AppError("report_date 应为 YYYY-MM-DD。") from None


async def owner_snapshot(day: date) -> dict:
    # Use the host client: existing authentication, request budget and error handling.
    athlete = get_config().athlete_id
    if not athlete:
        raise AppError("请先在现有 MCP 服务配置 ATHLETE_ID。")
    base = f"/athlete/{seg(canonical_athlete_id(athlete))}"
    snapshot: dict = {"date": day.isoformat()}
    for endpoint, oldest, newest in (
        ("wellness", day - timedelta(days=42), day),
        ("activities", day - timedelta(days=7), day),
        ("events", day, day + timedelta(days=6)),
    ):
        rows = await make_intervals_request(
            url=f"{base}/{endpoint}",
            params={"oldest": oldest.isoformat(), "newest": newest.isoformat()},
        )
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise AppError(f"读取 {endpoint} 失败或格式不符合预期，未生成报告。")
        snapshot[endpoint] = rows
    return snapshot


@tool("read")
async def get_daily_coach_summary(report_date: ReportDate = "") -> str:
    """Use to get a Chinese daily summary for the configured owner, for analysis in your own model/client. Includes recovery, yesterday's activities, 7-day load and calendar. Reads Intervals.icu only; does not call a model or send WeChat. Single-user mode only."""
    try:
        cfg = owner_config()
        day = report_day(cfg, report_date)
        return render_report(await owner_snapshot(day), day)
    except AppError as exc:
        return json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False)


@tool("write", annotations=EXTERNAL_WRITE)
async def analyze_daily_training(report_date: ReportDate = "") -> str:
    """Use to generate a Chinese AI daily report with the owner's configured model API. Sends a normalized training summary to LLM_BASE_URL and may incur provider charges. Returns the report without WeChat delivery or calendar changes. Requires write permission and single-user mode; never pass credentials as arguments."""
    try:
        cfg = owner_config()
        day = report_day(cfg, report_date)
        model = ModelConfig.from_env()
        snapshot = await owner_snapshot(day)
        return await asyncio.to_thread(analyze_snapshot, snapshot, day, model)
    except AppError as exc:
        return json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False)


@tool("write", annotations=EXTERNAL_WRITE)
async def send_daily_coach_report(
    report: Annotated[str, Field(description="Complete report body to send to the owner's configured WeChat")],
    report_date: ReportDate,
) -> str:
    """Use after the owner authorizes sending the complete coach report to their configured WeChat via ServerChan. Accepts only today's date in REPORT_TIMEZONE. At most one coach push attempt per day; an uncertain result requires manual review. Provider acceptance does not confirm handset delivery. Single-user mode only."""
    try:
        cfg = owner_config()
        result = await asyncio.to_thread(send_coach_report, cfg, report, report_date)
        return json.dumps(result, ensure_ascii=False)
    except AppError as exc:
        return json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False)
