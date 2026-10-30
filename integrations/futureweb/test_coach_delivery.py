"""Run with the integrated futureweb checkout's pytest environment."""
import json
from datetime import date
from unittest.mock import AsyncMock

import pytest
from mcp.server.fastmcp.exceptions import ToolError
from mcp.shared.memory import create_connected_server_and_client_session

from intervals_mcp_server import mcp_instance, tool_guard
from intervals_mcp_server.config import get_config
from mcp4intervals import futureweb
from mcp4intervals.core import Config


DAY = date(2026, 10, 30)
NAMES = {"get_daily_coach_summary", "analyze_daily_training", "send_daily_coach_report"}


@pytest.fixture(autouse=True)
def owner(monkeypatch, tmp_path):
    monkeypatch.setenv("MCP_TENANCY", "single")
    monkeypatch.setenv("REPORT_TIMEZONE", "Europe/Amsterdam")
    monkeypatch.setenv("COACH_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("SERVERCHAN_SENDKEY", "SCTexample")
    monkeypatch.setattr(get_config(), "athlete_id", "i123")
    monkeypatch.setattr(Config, "today", lambda self: DAY)
    monkeypatch.setattr(tool_guard, "activate_athlete_timezone", AsyncMock(return_value=None))
    monkeypatch.setattr(tool_guard, "reset_athlete_timezone", lambda value: None)


def scratch(scopes):
    server = mcp_instance.IntervalsFastMCP("integration-test")
    server.toolset = "full"
    server.permissions = frozenset({"read", "write"})
    server._token_scopes = lambda: scopes
    for name in NAMES:
        spec = mcp_instance._TOOL_SPECS[name]
        server.add_tool(spec.func, **spec.options)
    return server


@pytest.mark.asyncio
async def test_read_connection_cannot_analyze_or_push(monkeypatch):
    request = AsyncMock()
    monkeypatch.setattr(futureweb, "make_intervals_request", request)
    server = scratch(["intervals:read"])
    assert {item.name for item in await server.list_tools()} == {"get_daily_coach_summary"}
    for name, arguments in (("analyze_daily_training", {}), ("send_daily_coach_report", {"report": "report", "report_date": str(DAY)})):
        with pytest.raises(ToolError, match="write"):
            await server.call_tool(name, arguments)
    request.assert_not_awaited()


@pytest.mark.asyncio
async def test_write_catalogue_annotations_and_mcp_roundtrip(monkeypatch):
    monkeypatch.setattr(futureweb, "make_intervals_request", AsyncMock(return_value=[]))
    server = scratch(["intervals:read", "intervals:write"])
    tools = {item.name: item for item in await server.list_tools()}
    assert set(tools) == NAMES
    assert tools["analyze_daily_training"].annotations.openWorldHint is True
    assert tools["send_daily_coach_report"].annotations.readOnlyHint is False
    async with create_connected_server_and_client_session(server._mcp_server) as client:
        listed = await client.list_tools()
        assert {item.name for item in listed.tools} == NAMES
        result = await client.call_tool("get_daily_coach_summary", {"report_date": str(DAY)})
        assert not result.isError
        assert "2026-10-30" in result.content[0].text


@pytest.mark.asyncio
async def test_summary_uses_host_client_and_correct_ranges(monkeypatch):
    request = AsyncMock(return_value=[])
    monkeypatch.setattr(futureweb, "make_intervals_request", request)
    report = await futureweb.get_daily_coach_summary(str(DAY))
    assert "睡眠：未知" in report
    assert request.await_count == 3
    calls = request.await_args_list
    assert calls[0].kwargs == {"url": "/athlete/i123/wellness", "params": {"oldest": "2026-09-18", "newest": "2026-10-30"}}
    assert calls[1].kwargs["params"]["oldest"] == "2026-10-23"
    assert calls[2].kwargs["params"]["newest"] == "2026-11-05"


@pytest.mark.asyncio
async def test_model_analysis_returns_without_sending(monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "https://model.example/v1")
    monkeypatch.setenv("LLM_MODEL", "test-model")
    monkeypatch.setattr(futureweb, "make_intervals_request", AsyncMock(return_value=[]))
    analyzed = []
    monkeypatch.setattr(futureweb, "analyze_snapshot", lambda *args: analyzed.append(args) or "AI报告")
    push = AsyncMock()
    monkeypatch.setattr(futureweb, "send_coach_report", push)
    assert await futureweb.analyze_daily_training(str(DAY)) == "AI报告"
    assert analyzed[0][2].model == "test-model"
    push.assert_not_called()


@pytest.mark.asyncio
async def test_failed_fetch_does_not_call_model(monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "https://model.example/v1")
    monkeypatch.setenv("LLM_MODEL", "test-model")
    monkeypatch.setattr(futureweb, "make_intervals_request", AsyncMock(return_value={"error": True}))
    model = AsyncMock()
    monkeypatch.setattr(futureweb, "analyze_snapshot", model)
    result = json.loads(await futureweb.analyze_daily_training(str(DAY)))
    assert result["status"] == "error"
    model.assert_not_called()


@pytest.mark.asyncio
async def test_multi_user_refused_before_read_model_or_push(monkeypatch):
    monkeypatch.setenv("MCP_TENANCY", "multi")
    request = AsyncMock()
    monkeypatch.setattr(futureweb, "make_intervals_request", request)
    # Direct unwrapped functions verify the adapter's own refusal too.
    for func, args in ((futureweb.get_daily_coach_summary, ()), (futureweb.analyze_daily_training, ()), (futureweb.send_daily_coach_report, ("report", str(DAY)))):
        result = json.loads(await func.__wrapped__(*args))
        assert result["status"] == "error"
        assert "single" in result["message"]
    request.assert_not_called()


@pytest.mark.asyncio
async def test_push_idempotency_and_stale_date(monkeypatch):
    sent = []
    monkeypatch.setattr("mcp4intervals.core.send_serverchan", lambda *args: sent.append(args))
    # deliver's default sender is bound when imported: exercise real delivery with a stub transport.
    monkeypatch.setattr("mcp4intervals.core.request_json", lambda *args, **kwargs: sent.append(kwargs) or {"code": 0})
    first = json.loads(await futureweb.send_daily_coach_report("报告", str(DAY)))
    second = json.loads(await futureweb.send_daily_coach_report("报告", str(DAY)))
    stale = json.loads(await futureweb.send_daily_coach_report("旧报告", "2026-10-29"))
    assert first["status"] == "accepted_by_serverchan"
    assert second["status"] == "already_sent"
    assert stale["status"] == "error"
    assert len(sent) == 1
