from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import mean
from typing import Callable
from zoneinfo import ZoneInfo

from .ilink import ILinkClient, ILinkError

ROOT = Path(__file__).resolve().parent.parent


class AppError(Exception):
    """A message safe to print without credentials or remote response bodies."""


class ContextExpired(AppError):
    """The WeChat session context is gone; retry after the user messages the bot."""


def load_env(path: Path) -> None:
    """Simple KEY=value files; no shell expansion, no overwriting process env."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key.strip()):
            raise AppError(".env 格式错误：每行应为 KEY=value。")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


@dataclass(frozen=True)
class Config:
    api_key: str = ""
    athlete_id: str = "0"
    bot_token: str = ""
    ilink_bot_id: str = ""
    ilink_user_id: str = ""
    base_url: str = "https://ilinkai.weixin.qq.com"
    context_token: str = ""
    timezone: str = "Europe/Amsterdam"
    send_at: str = "07:00"
    state_dir: Path = ROOT / "data"
    report_mode: str = "summary"

    @classmethod
    def from_env(cls) -> Config:
        cfg = cls(
            api_key=os.getenv("INTERVALS_API_KEY", ""),
            athlete_id=os.getenv("INTERVALS_ATHLETE_ID", "0"),
            bot_token=os.getenv("ILINK_BOT_TOKEN", ""),
            ilink_bot_id=os.getenv("ILINK_BOT_ID", ""),
            ilink_user_id=os.getenv("ILINK_USER_ID", ""),
            base_url=os.getenv("ILINK_BASE_URL", "https://ilinkai.weixin.qq.com"),
            context_token=os.getenv("ILINK_CONTEXT_TOKEN", ""),
            timezone=os.getenv("REPORT_TIMEZONE", "Europe/Amsterdam"),
            send_at=os.getenv("REPORT_TIME", "07:00"),
            state_dir=Path(os.getenv("STATE_DIR", str(ROOT / "data"))),
            report_mode=os.getenv("REPORT_MODE", "summary"),
        )
        if cfg.report_mode not in ("summary", "ai"):
            raise AppError("REPORT_MODE 应为 summary 或 ai。")
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", cfg.send_at):
            raise AppError("REPORT_TIME 应为 HH:MM，例如 07:00。")
        if not re.fullmatch(r"i?\d+", cfg.athlete_id):
            raise AppError("INTERVALS_ATHLETE_ID 应为数字或 i 开头的数字。")
        try:
            ZoneInfo(cfg.timezone)
        except (KeyError, ValueError):
            raise AppError("时区不可用，请安装 requirements.txt 中的 tzdata。") from None
        return cfg

    def today(self) -> date:
        return datetime.now(ZoneInfo(self.timezone)).date()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Keep Intervals Basic auth and the iLink token on their original hosts.
        return None


def request_json(url: str, *, data: bytes | None = None, headers=None, timeout=30):
    request = urllib.request.Request(
        url, data=data, headers=headers or {}, method="POST" if data is not None else "GET"
    )
    try:
        opener = urllib.request.build_opener(NoRedirect())
        with opener.open(request, timeout=timeout) as response:
            return json.loads(response.read(2_000_001))
    except urllib.error.HTTPError as exc:
        # Never print the exception: it may embed request details.
        raise AppError(f"远程请求返回 HTTP {exc.code}，请检查凭据、权限及服务状态。") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise AppError("远程请求未能确认成功，请检查网络；推送超时不自动重发。") from None
    except (ValueError, UnicodeError):
        raise AppError("远程服务返回了无法解析的 JSON。") from None


def fetch_snapshot(cfg: Config, day: date) -> dict:
    if not cfg.api_key or cfg.api_key.startswith("replace_"):
        raise AppError("请先在 .env 填写 INTERVALS_API_KEY。")
    basic = base64.b64encode(f"API_KEY:{cfg.api_key}".encode()).decode()
    headers = {"Authorization": f"Basic {basic}", "Accept": "application/json"}
    base = f"https://intervals.icu/api/v1/athlete/{cfg.athlete_id}"

    def get_list(endpoint, oldest, newest):
        query = urllib.parse.urlencode({"oldest": oldest.isoformat(), "newest": newest.isoformat()})
        result = request_json(f"{base}/{endpoint}?{query}", headers=headers)
        if not isinstance(result, list) or any(not isinstance(x, dict) for x in result):
            raise AppError(f"Intervals.icu 的 {endpoint} 返回格式不符合预期。")
        return result

    # Finished local days, including all activity types; do not infer completion %.
    return {
        "date": day.isoformat(),
        "wellness": get_list("wellness", day - timedelta(days=42), day),
        "activities": get_list("activities", day - timedelta(days=7), day),
        "events": get_list("events", day, day + timedelta(days=6)),
    }


def build_ai_coach_event(workout: dict, day: date) -> dict:
    """Normalize a model-proposed workout into an Intervals.icu event.

    The title is always prefixed with "AI Coach"; the date defaults to
    tomorrow and the sport to Ride. Only fields Intervals accepts are sent.
    """
    name = str(workout.get("name") or "训练计划").strip() or "训练计划"
    if not name.lower().startswith("ai coach"):
        name = "AI Coach: " + name
    raw_date = str(workout.get("date") or "").strip()
    try:
        event_date = date.fromisoformat(raw_date)
    except ValueError:
        event_date = day + timedelta(days=1)
    event = {
        "category": "WORKOUT",
        "type": str(workout.get("type") or "Ride").strip() or "Ride",
        "name": name[:80],
        "start_date_local": event_date.isoformat() + "T00:00:00",
        "description": str(workout.get("description") or "").strip(),
    }
    moving_time = workout.get("moving_time")
    if isinstance(moving_time, int) and not isinstance(moving_time, bool) and 0 < moving_time <= 24 * 3600:
        event["moving_time"] = moving_time
    return event


def create_event(cfg: Config, event: dict) -> dict:
    """Create one calendar event (course/workout) on Intervals.icu."""
    if not cfg.api_key or cfg.api_key.startswith("replace_"):
        raise AppError("请先填写 INTERVALS_API_KEY。")
    basic = base64.b64encode(f"API_KEY:{cfg.api_key}".encode()).decode()
    headers = {"Authorization": f"Basic {basic}", "Accept": "application/json",
               "Content-Type": "application/json"}
    url = f"https://intervals.icu/api/v1/athlete/{cfg.athlete_id}/events"
    result = request_json(url, data=json.dumps(event, ensure_ascii=False).encode("utf-8"),
                          headers=headers)
    if not isinstance(result, dict):
        raise AppError("Intervals.icu 创建课程返回格式不符合预期。")
    return result


def number(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(value):
        return value
    return None


def scalar(value, unit=""):
    n = number(value)
    return "未知" if n is None else f"{n:g}{unit}"


def duration(seconds):
    n = number(seconds)
    if n is None or n < 0:
        return "未知"
    minutes = int(n // 60)
    return f"{minutes // 60}小时{minutes % 60}分"


def baseline(rows: list[dict], day: date, field: str):
    cutoff = (day - timedelta(days=42)).isoformat()
    values = [number(row.get(field)) for row in rows
              if cutoff <= str(row.get("id", "")) < day.isoformat()]
    values = [x for x in values if x is not None and x > 0]
    return (mean(values) if values else None), len(values)


def render_report(snapshot: dict, day: date) -> str:
    if snapshot.get("date") != day.isoformat():
        raise AppError("数据日期与报告日期不一致，拒绝把旧快照当作今日数据。")
    wellness = snapshot.get("wellness", [])
    activities = snapshot.get("activities", [])
    events = snapshot.get("events", [])
    today: dict = next((row for row in wellness if row.get("id") == day.isoformat()), {})
    rest_mean, rest_n = baseline(wellness, day, "restingHR")
    _, hrv_n = baseline(wellness, day, "hrv")
    yesterday = (day - timedelta(days=1)).isoformat()
    week_start = (day - timedelta(days=7)).isoformat()
    past = [x for x in activities if week_start <= str(x.get("start_date_local", ""))[:10] < day.isoformat()]
    yesterday_rides = [x for x in past if str(x.get("start_date_local", ""))[:10] == yesterday]
    loads = [number(x.get("icu_training_load")) for x in past]
    missing_loads = sum(x is None for x in loads)
    load_total = sum(x for x in loads if x is not None)
    rest_note = (f"此前42天有效记录均值 {rest_mean:.1f} bpm，n={rest_n}"
                 if rest_mean is not None else "此前42天无有效基线")
    lines = [
        f"# 骑行日报 · {day.isoformat()}", "",
        "来源：Intervals.icu；这是程序生成的数据摘要。", "",
        "## 今日恢复记录", "",
        f"- 睡眠：{duration(today.get('sleepSecs'))}；评分：{scalar(today.get('sleepScore'))}",
        f"- 静息心率：{scalar(today.get('restingHR'), ' bpm')}（{rest_note}）",
        f"- HRV：{scalar(today.get('hrv'), ' ms')}；此前42天有效记录 {hrv_n} 天",
        f"- 主观疲劳（平台原始评分）：{scalar(today.get('fatigue'))}",
        "- HRV 测量来源与方法未核实；这里仅展示数值，不据此判定恢复或增加强度。", "",
        "## 昨日活动", "",
    ]
    if not yesterday_rides:
        lines.append("未见已同步活动；不等于确认没有训练。")
    for ride in sorted(yesterday_rides, key=lambda x: str(x.get("start_date_local", ""))):
        distance = number(ride.get("distance"))
        distance_text = "未知" if distance is None else f"{distance / 1000:.1f} km"
        lines.append(f"- {ride.get('name') or '未命名活动'}：{duration(ride.get('moving_time'))}，"
                     f"距离 {distance_text}，NP {scalar(ride.get('icu_weighted_avg_watts'), ' W')}，"
                     f"负荷 {scalar(ride.get('icu_training_load'))}")
    lines.extend(["", f"过去7个完整当地日：{len(past)}次活动，已知训练负荷合计 {load_total:g}。"
                  f"其中 {missing_loads} 次负荷缺失。", "", "## 含今日在内未来7天日历", ""])
    for offset in range(7):
        plan_day = (day + timedelta(days=offset)).isoformat()
        items = [x for x in events if str(x.get("start_date_local", ""))[:10] == plan_day]
        if not items:
            lines.append(f"- {plan_day}：日历无记录，请以现有教练安排为准。")
        for event in sorted(items, key=lambda x: str(x.get("start_date_local", ""))):
            lines.append(f"- {plan_day}：{event.get('name') or '未命名日历事件'}"
                         f"（{event.get('category') or '类型未知'}，计划时长 {duration(event.get('moving_time'))}）")
    lines.extend(["", "本摘要未修改 FTP 或训练日历，也未判断整课完成率。",
                  "如需原有 AI 教练的完整分析，请由教练调用推送工具发送原文。"])
    return "\n".join(lines) + "\n"


def send_ilink(cfg: Config, title: str, body: str) -> None:
    if not cfg.bot_token or not cfg.ilink_user_id:
        raise AppError("尚未绑定微信机器人（缺少 iLink 凭据），请先在开通页扫码绑定。")
    if not cfg.context_token:
        raise ContextExpired("微信会话上下文已失效，请让该用户给机器人发一条消息后再试。")
    client = ILinkClient(base_url=cfg.base_url, token=cfg.bot_token)
    try:
        client.send_text(cfg.ilink_user_id, cfg.context_token, f"{title}\n{body}")
    except ILinkError as exc:
        if exc.code in (-2, -14):
            raise ContextExpired("微信会话上下文已失效，需用户重新发消息或重新扫码。") from None
        raise AppError("微信推送未确认成功，请稍后核对微信是否收到。") from None


def deliver(cfg: Config, day: date, title: str, body: str, *, kind="daily",
            sender: Callable = send_ilink) -> dict:
    """Reserve a daily slot before sending; ambiguous failures require manual review.

    SQLite makes overlapping processes atomic. This is at-most-one automatic attempt,
    not exactly-once delivery: an accepted push followed by a crash is ambiguous.
    """
    if not title.strip() or not body.strip():
        raise AppError("标题和消息正文不能为空。")
    if len(title) > 80 or len(body.encode("utf-8")) > 28_000:
        raise AppError("消息过长：标题最多80字符，正文最多28000 UTF-8字节；请先压缩摘要。")
    if not re.fullmatch(r"[a-z0-9_-]{1,32}", kind):
        raise AppError("消息类型格式不正确。")
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    db_path = cfg.state_dir / "deliveries.sqlite3"
    db = sqlite3.connect(db_path, timeout=10)
    try:
        db.execute("CREATE TABLE IF NOT EXISTS deliveries (slot TEXT PRIMARY KEY, status TEXT NOT NULL, digest TEXT NOT NULL, updated TEXT NOT NULL)")
        # Namespace by destination, never store the secret itself.
        destination = hashlib.sha256(cfg.ilink_user_id.encode()).hexdigest()[:16]
        slot = f"{destination}:{kind}:{day.isoformat()}"
        digest = hashlib.sha256(body.encode()).hexdigest()
        with db:
            reserved = db.execute("INSERT OR IGNORE INTO deliveries VALUES (?, 'sending', ?, ?)",
                                  (slot, digest, datetime.now().isoformat())).rowcount
        if not reserved:
            status = db.execute("SELECT status FROM deliveries WHERE slot=?", (slot,)).fetchone()[0]
            if status == "sent":
                return {"status": "already_sent", "date": day.isoformat()}
            raise AppError("当天这类消息已有一次未确认的推送，请核对微信，不自动重试。")
        try:
            sender(cfg, title, body)
        except ContextExpired:
            # Retryable: the WeChat session context expired. Roll the slot back
            # so the daemon can retry once the user messages the bot again.
            with db:
                db.execute("DELETE FROM deliveries WHERE slot=?", (slot,))
            raise
        except Exception:
            with db:
                db.execute("UPDATE deliveries SET status='uncertain', updated=? WHERE slot=?",
                           (datetime.now().isoformat(), slot))
            raise AppError("推送未确认成功；请核对微信。为避免重复，当天不自动重发。") from None
        with db:
            db.execute("UPDATE deliveries SET status='sent', updated=? WHERE slot=?",
                       (datetime.now().isoformat(), slot))
        return {"status": "accepted_by_ilink", "date": day.isoformat()}
    finally:
        db.close()


def delivery_status(cfg: Config, day: date, *, kind="daily") -> dict | None:
    """Skip expensive data/model calls when today's delivery slot is occupied.

    This is a read-only optimization; deliver still reserves atomically.
    """
    path = cfg.state_dir / "deliveries.sqlite3"
    if not path.exists():
        return None
    db = sqlite3.connect(path, timeout=10)
    try:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='deliveries'").fetchone():
            return None
        destination = hashlib.sha256(cfg.ilink_user_id.encode()).hexdigest()[:16]
        row = db.execute("SELECT status FROM deliveries WHERE slot=?",
                         (f"{destination}:{kind}:{day.isoformat()}",)).fetchone()
        if row is None:
            return None
        if row[0] == "sent":
            return {"status": "already_sent", "date": day.isoformat()}
        raise AppError("当天这类消息已有一次未确认的推送，请核对微信，不自动重试。")
    finally:
        db.close()


def run_report(cfg: Config, day: date, mode: str | None = None, model=None) -> dict:
    """Fetch, build and deliver one athlete's daily report; return the delivery status.

    "mode" overrides "cfg.report_mode". A data or model failure raises without sending;
    a push failure raises without retrying (the delivery slot stays reserved).
    """
    mode = mode or cfg.report_mode
    existing = delivery_status(cfg, day)
    if existing:
        return existing
    snapshot = fetch_snapshot(cfg, day)
    if mode == "ai":
        from .ai import analyze_snapshot
        report = analyze_snapshot(snapshot, day, model)
    else:
        report = render_report(snapshot, day)
    return deliver(cfg, day, f"骑行日报 {day.isoformat()}", report)


def scheduled_tick(cfg: Config, now: datetime, run: Callable) -> bool:
    """Send once after scheduled local time, including catch-up after restart."""
    if now.tzinfo is None:
        raise AppError("调度时间必须包含时区。")
    local = now.astimezone(ZoneInfo(cfg.timezone))
    hour, minute = map(int, cfg.send_at.split(":"))
    if (local.hour, local.minute) < (hour, minute):
        return False
    run(local.date())
    return True
