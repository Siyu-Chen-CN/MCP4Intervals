from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .ai import ModelError
from .core import (ROOT, AppError, Config, ContextExpired, deliver, delivery_status, fetch_snapshot,
                   load_env, render_report, run_report, scheduled_tick)
from .ilink import ILinkClient, ILinkError
from .users import UserStore, canonical_athlete, default_users_db, users_from_json


def _reconfigure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass


def handle_user(args) -> int:
    store = UserStore(args.users_db or default_users_db())
    if args.user_command == "import":
        data = json.loads(args.file.read_text(encoding="utf-8-sig"))
        for user in users_from_json(data):
            store.upsert(user)
            print(json.dumps({"status": "saved", "athlete_id": user.athlete_id}, ensure_ascii=False))
        return 0
    if args.user_command == "list":
        for user in store.all():
            print(json.dumps(user.public_summary(), ensure_ascii=False))
        return 0
    if args.user_command == "remove":
        removed = store.remove(args.athlete_id)
        print(json.dumps({"status": "removed" if removed else "not_found",
                          "athlete_id": canonical_athlete(args.athlete_id)}, ensure_ascii=False))
        return 0
    if args.user_command in ("enable", "disable"):
        user = store.set_enabled(args.athlete_id, args.user_command == "enable")
        print(json.dumps(user.public_summary(), ensure_ascii=False))
        return 0
    raise AppError("未知的用户子命令。")


def _poll_user(store: UserStore, user) -> str | None:
    """Poll one user's bot for inbound messages; return a newly seen context_token."""
    state = store.get_ilink_state(user.athlete_id)
    client = ILinkClient(base_url=user.base_url, token=user.bot_token)
    try:
        updates = client.get_updates(state["cursor"], timeout=3)
    except ILinkError as exc:
        if exc.code == -14:
            print(f"微信会话已失效（{user.athlete_id}），需要重新扫码绑定。", file=sys.stderr, flush=True)
        return None
    store.set_ilink_state(user.athlete_id, cursor=updates["cursor"])
    context = None
    for msg in updates["msgs"]:
        if msg.get("from_user_id") != user.ilink_user_id:
            continue
        if msg.get("group_id") or msg.get("message_type") != 1 or msg.get("message_state") != 2:
            continue
        token = msg.get("context_token")
        if token:
            context = token
    if context:
        store.set_ilink_state(user.athlete_id, context_token=context)
    return context


def _send_for_user(store: UserStore, user, state_dir: Path, mode: str | None, handled: dict) -> None:
    if not user.enabled or not user.wechat_bound:
        return
    local = datetime.now(ZoneInfo(user.timezone))
    hour, minute = map(int, user.send_at.split(":"))
    if (local.hour, local.minute) < (hour, minute):
        return
    day = local.date()
    if handled.get(user.athlete_id) == day:
        return
    effective = mode or user.report_mode
    if effective == "ai" and user.model is None:
        handled[user.athlete_id] = day
        print(f"AI 日报未完成（{user.athlete_id}）：未配置模型 API。", file=sys.stderr, flush=True)
        return
    context = store.get_ilink_state(user.athlete_id)["context_token"]
    if not context:
        return  # 等用户发消息建立会话
    cfg = replace(user.to_config(state_dir), context_token=context)
    try:
        result = run_report(cfg, day, mode, model=user.model)
        handled[user.athlete_id] = day
        print(json.dumps({"athlete": user.athlete_id, "date": day.isoformat(), **result},
                         ensure_ascii=False), flush=True)
    except ContextExpired:
        print(f"日报等待 {user.athlete_id} 给机器人发消息建立会话。", flush=True)
    except ModelError as exc:
        handled[user.athlete_id] = day
        print(f"AI 日报未完成（{user.athlete_id}）：{exc} 今天不再自动调用其模型。", file=sys.stderr, flush=True)
    except AppError as exc:
        handled[user.athlete_id] = day
        print(f"日报未完成（{user.athlete_id}）：{exc}", file=sys.stderr, flush=True)


def run_multidaemon(store: UserStore, state_dir: Path, mode: str | None = None) -> None:
    """Push each user's daily report at their local time; harvest context via polling.

    iLink cannot send proactively without a context_token, so each user must first
    message their bot. The daemon polls getupdates to harvest that token, pushes on
    schedule when a token is available, and re-pushes a missed report when a new
    inbound message refreshes the context.
    """
    handled: dict[str, date] = {}
    print("多用户日报进程就绪：定时推送 + 微信轮询；用户需先给机器人发一条消息。", flush=True)
    last_tick = 0.0
    while True:
        users = [u for u in store.all() if u.enabled and u.wechat_bound]
        for user in users:
            try:
                new_context = _poll_user(store, user)
            except Exception:
                continue
            if new_context:
                _send_for_user(store, user, state_dir, mode, handled)
        now = time.time()
        if now - last_tick >= 60:
            last_tick = now
            for user in users:
                _send_for_user(store, user, state_dir, mode, handled)
        time.sleep(2)


def main():
    _reconfigure_stdio()
    parser = argparse.ArgumentParser(description="Intervals.icu 中文日报 / 微信推送草稿")
    parser.add_argument("--env", type=Path, default=ROOT / ".env")
    commands = parser.add_subparsers(dest="command", required=True)
    for cmd in ("preview", "send", "daemon"):
        sub = commands.add_parser(cmd)
        sub.add_argument("--mode", choices=("summary", "ai"), help="覆盖 REPORT_MODE；ai 调用用户配置的模型 API")
        if cmd != "daemon":
            sub.add_argument("--date", type=date.fromisoformat)
            sub.add_argument("--fixture", type=Path, help="仅供离线预览；发送命令禁用")
    send_file = commands.add_parser("send-file")
    send_file.add_argument("file", type=Path)
    send_file.add_argument("--date", type=date.fromisoformat)
    send_file.add_argument("--title", default="每日骑行教练")
    commands.add_parser("test-push")
    commands.add_parser("mcp-stdio")
    multidaemon = commands.add_parser("multidaemon")
    multidaemon.add_argument("--users-db", type=Path, default=None)
    multidaemon.add_argument("--state-dir", type=Path, default=None)
    multidaemon.add_argument("--mode", choices=("summary", "ai"), help="覆盖所有用户的 report_mode")
    user = commands.add_parser("user")
    user_sub = user.add_subparsers(dest="user_command", required=True)
    user_import = user_sub.add_parser("import", help="从 JSON 文件导入或更新用户")
    user_import.add_argument("file", type=Path)
    user_import.add_argument("--users-db", type=Path, default=None)
    user_list = user_sub.add_parser("list", help="列出用户（不含密钥）")
    user_list.add_argument("--users-db", type=Path, default=None)
    for name in ("remove", "enable", "disable"):
        sub = user_sub.add_parser(name, help={"remove": "删除用户", "enable": "启用用户", "disable": "停用用户"}[name])
        sub.add_argument("athlete_id")
        sub.add_argument("--users-db", type=Path, default=None)
    web = commands.add_parser("web", help="启动自助开通网页")
    web.add_argument("--host", default=None)
    web.add_argument("--port", type=int, default=None)
    web.add_argument("--users-db", type=Path, default=None)
    args = parser.parse_args()
    try:
        load_env(args.env)
        if args.command == "user":
            return handle_user(args)
        if args.command == "multidaemon":
            store = UserStore(args.users_db or default_users_db())
            state_dir = args.state_dir or Path(os.getenv("STATE_DIR", str(ROOT / "data")))
            run_multidaemon(store, state_dir, args.mode)
            return 0
        if args.command == "web":
            from .web import run as run_web
            store = UserStore(args.users_db or default_users_db())
            run_web(store, args.host, args.port)
            return 0
        cfg = Config.from_env()
        if args.command == "mcp-stdio":
            from .mcp_stdio import run
            run(cfg)
            return 0

        def daily(day):
            fixture = getattr(args, "fixture", None)
            if fixture and args.command != "preview":
                raise AppError("演示数据只能预览，不能作为真实日报发送。")
            if args.command != "preview":
                existing = delivery_status(cfg, day)
                if existing:
                    print(json.dumps(existing, ensure_ascii=False), flush=True)
                    return
            snapshot = (json.loads(fixture.read_text(encoding="utf-8-sig")) if fixture
                        else fetch_snapshot(cfg, day))
            mode = args.mode or cfg.report_mode
            if mode == "ai":
                from .ai import analyze_snapshot
                report = analyze_snapshot(snapshot, day)
            else:
                report = render_report(snapshot, day)
            if args.command == "preview":
                print(report)
                return
            result = deliver(cfg, day, f"骑行日报 {day.isoformat()}", report)
            print(json.dumps(result, ensure_ascii=False), flush=True)

        if args.command == "daemon":
            print(f"日报进程就绪：{cfg.timezone} 每天 {cfg.send_at}；模式 {args.mode or cfg.report_mode}；计划时间后启动将尝试补发当日报告。", flush=True)
            # Retry data fetch failures; don't repeatedly charge a failing model.
            # A push failure is reserved in SQLite so no second push is attempted.
            handled_day = None
            while True:
                now = datetime.now(ZoneInfo(cfg.timezone))
                if handled_day != now.date():
                    try:
                        if scheduled_tick(cfg, now, daily):
                            handled_day = now.date()
                    except ModelError as exc:
                        handled_day = now.date()
                        print(f"AI 日报未完成：{exc} 本进程今天不再自动调用模型；修复后可手动运行 send --mode ai。", file=sys.stderr, flush=True)
                    except AppError as exc:
                        print(f"日报未完成：{exc}", file=sys.stderr, flush=True)
                time.sleep(60)
        elif args.command == "send-file":
            day = args.date or cfg.today()
            if day != cfg.today():
                raise AppError("原文推送仅接受今天的日期，避免把旧报告误作新消息。")
            print(json.dumps(deliver(cfg, day, args.title,
                                     args.file.read_text(encoding="utf-8-sig"), kind="coach"), ensure_ascii=False))
        elif args.command == "test-push":
            print(json.dumps(deliver(cfg, cfg.today(), "MCP4Intervals 微信连接测试",
                                     "这是一条连接测试消息，不包含训练数据。", kind="test"), ensure_ascii=False))
        else:
            day = args.date or cfg.today()
            if args.command == "send" and day != cfg.today():
                raise AppError("自动摘要仅发送今天的数据；历史日期请用 preview。")
            daily(day)
        return 0
    except KeyboardInterrupt:
        return 0
    except (AppError, OSError, ValueError) as exc:
        # AppError messages are sanitized. Never dump raw OS/JSON exceptions.
        message = str(exc) if isinstance(exc, AppError) else "本地文件或配置读取失败，请检查路径和格式。"
        print(f"错误：{message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
