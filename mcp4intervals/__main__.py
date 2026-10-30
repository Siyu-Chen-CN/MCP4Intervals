from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .ai import ModelError
from .core import (ROOT, AppError, Config, deliver, delivery_status, fetch_snapshot, load_env,
                   render_report, scheduled_tick)


def main():
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
    args = parser.parse_args()
    try:
        load_env(args.env)
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
