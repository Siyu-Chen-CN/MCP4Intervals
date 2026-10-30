"""Per-user registry for the self-service multi-user daily report.

One SQLite row per athlete holds their own Intervals.icu API key, their
WeChat iLink bot credentials (the WeChat destination), their optional model
API and their daily schedule. The onboarding app (web page or "user import")
writes here; the multi-user daemon reads here.

Secrets (API keys, bot tokens) live in the database file (0600) and are never
printed by list/status views; error messages name only field names, not values.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .ai import ModelConfig
from .core import ROOT, AppError, Config


def canonical_athlete(value: Any) -> str:
    """Intervals.icu athlete id in its canonical i123 form."""
    text = str(value).strip().lower()
    if text.startswith("i") and text[1:].isdigit():
        text = text[1:]
    return f"i{text}" if text.isdigit() else str(value).strip()


def _valid_athlete(value: str) -> bool:
    return value.startswith("i") and len(value) > 1 and value[1:].isdigit()


def _valid_time(value: str) -> bool:
    parts = value.split(":")
    if len(parts) != 2 or len(parts[0]) != 2 or len(parts[1]) != 2:
        return False
    if not parts[0].isdigit() or not parts[1].isdigit():
        return False
    return 0 <= int(parts[0]) <= 23 and 0 <= int(parts[1]) <= 59


def default_users_db() -> Path:
    raw = os.getenv("USERS_DB", "").strip()
    if raw:
        return Path(raw)
    state = os.getenv("STATE_DIR", "").strip()
    base = Path(state) if state else ROOT / "data"
    return base / "users.sqlite3"


_PBKDF2_ITERATIONS = 120_000


def hash_secret(secret: str) -> tuple[str, str]:
    """(salt_hex, digest_hex) for a manage secret; PBKDF2-HMAC-SHA256, salt per user."""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", secret.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return salt.hex(), digest.hex()


def verify_secret(secret: str, salt_hex: str, hash_hex: str) -> bool:
    try:
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except ValueError:
        return False
    actual = hashlib.pbkdf2_hmac("sha256", secret.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return hmac.compare_digest(actual, expected)


@dataclass(frozen=True)
class User:
    athlete_id: str
    intervals_api_key: str
    bot_token: str = ""
    ilink_bot_id: str = ""
    ilink_user_id: str = ""
    base_url: str = "https://ilinkai.weixin.qq.com"
    timezone: str = "Europe/Amsterdam"
    send_at: str = "07:00"
    report_mode: str = "summary"
    model: ModelConfig | None = None
    enabled: bool = True

    @property
    def wechat_bound(self) -> bool:
        return bool(self.bot_token and self.ilink_user_id)

    def to_config(self, state_dir: Path) -> Config:
        return Config(
            api_key=self.intervals_api_key,
            athlete_id=self.athlete_id,
            bot_token=self.bot_token,
            ilink_bot_id=self.ilink_bot_id,
            ilink_user_id=self.ilink_user_id,
            base_url=self.base_url,
            timezone=self.timezone,
            send_at=self.send_at,
            state_dir=state_dir,
            report_mode=self.report_mode,
        )

    def public_summary(self) -> dict:
        """Non-secret fields only, for "user list"."""
        return {
            "athlete_id": self.athlete_id,
            "enabled": self.enabled,
            "timezone": self.timezone,
            "send_at": self.send_at,
            "report_mode": self.report_mode,
            "model": None if self.model is None else self.model.model,
        }

    def public_detail(self) -> dict:
        """Non-secret fields for the onboarding page (never keys or bot tokens)."""
        return {
            "athlete_id": self.athlete_id,
            "enabled": self.enabled,
            "timezone": self.timezone,
            "send_at": self.send_at,
            "report_mode": self.report_mode,
            "model_name": None if self.model is None else self.model.model,
            "model_base_url": None if self.model is None else self.model.base_url,
            "model_timeout": None if self.model is None else self.model.timeout,
            "model_max_tokens": None if self.model is None else self.model.max_tokens,
            "model_allow_http": False if self.model is None else self.model.allow_http,
            "intervals_api_key_set": bool(self.intervals_api_key),
            "wechat_bound": self.wechat_bound,
            "model_api_key_set": bool(self.model is not None and bool(self.model.api_key)),
        }


def _require(data: dict, key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise AppError(f"用户配置缺少或为空：{key}。")
    return value.strip()


def _parse_int(data: dict, key: str, default: int) -> int:
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise AppError(f"用户配置的 {key} 应为整数。")
    return value


def _parse_model(spec: Any) -> ModelConfig:
    if not isinstance(spec, dict):
        raise AppError("用户配置的 model 应为对象。")
    base_url = _require(spec, "base_url")
    model = _require(spec, "model")
    api_key = spec.get("api_key", "")
    if not isinstance(api_key, str):
        raise AppError("用户配置的 model.api_key 应为字符串。")
    cfg = ModelConfig(
        base_url=base_url,
        model=model,
        api_key=api_key.strip(),
        timeout=_parse_int(spec, "timeout", 120),
        max_tokens=_parse_int(spec, "max_tokens", 2500),
        allow_http=spec.get("allow_http", False),
    )
    try:
        cfg.validate()
    except AppError as exc:
        raise AppError(f"用户配置的模型参数无效：{exc}") from None
    return cfg


def make_user(data: dict) -> User:
    if not isinstance(data, dict):
        raise AppError("用户条目应为 JSON 对象。")
    athlete = canonical_athlete(_require(data, "athlete_id"))
    if not _valid_athlete(athlete):
        raise AppError("athlete_id 应为数字或 i 开头的数字。")
    api_key = _require(data, "intervals_api_key")
    if api_key.startswith("replace_"):
        raise AppError("请填写有效的 intervals_api_key。")

    def _opt_str(key: str) -> str:
        value = data.get(key, "")
        if not isinstance(value, str):
            raise AppError(f"用户配置的 {key} 应为字符串。")
        return value.strip()

    bot_token = _opt_str("bot_token")
    ilink_bot_id = _opt_str("ilink_bot_id")
    ilink_user_id = _opt_str("ilink_user_id")
    base_url = _opt_str("base_url") or "https://ilinkai.weixin.qq.com"
    timezone_ = data.get("timezone", "Europe/Amsterdam")
    if not isinstance(timezone_, str) or not timezone_.strip():
        timezone_ = "Europe/Amsterdam"
    timezone_ = timezone_.strip()
    try:
        ZoneInfo(timezone_)
    except (KeyError, ValueError):
        raise AppError("timezone 不可用，请使用 IANA 时区名。") from None
    send_at = data.get("send_at", "07:00")
    if not isinstance(send_at, str) or not _valid_time(send_at.strip()):
        raise AppError("send_at 应为 HH:MM，例如 07:00。")
    report_mode = data.get("report_mode", "summary")
    if not isinstance(report_mode, str) or report_mode.strip().lower() not in ("summary", "ai"):
        raise AppError("report_mode 应为 summary 或 ai。")
    model_spec = data.get("model")
    model = _parse_model(model_spec) if model_spec not in (None, "") else None
    enabled = data.get("enabled", True)
    if not isinstance(enabled, bool):
        raise AppError("enabled 应为布尔值。")
    return User(
        athlete_id=athlete,
        intervals_api_key=api_key,
        bot_token=bot_token,
        ilink_bot_id=ilink_bot_id,
        ilink_user_id=ilink_user_id,
        base_url=base_url,
        timezone=timezone_,
        send_at=send_at.strip(),
        report_mode=report_mode.strip().lower(),
        model=model,
        enabled=enabled,
    )


def users_from_json(data: Any) -> list[User]:
    if isinstance(data, dict) and "users" in data:
        data = data["users"]
    if isinstance(data, dict):
        return [make_user(data)]
    if isinstance(data, list):
        return [make_user(item) for item in data]
    raise AppError("用户文件应为 JSON 对象、对象数组或含 users 键的对象。")


def _serialize(user: User) -> dict:
    return {
        "athlete_id": user.athlete_id,
        "intervals_api_key": user.intervals_api_key,
        "bot_token": user.bot_token,
        "ilink_bot_id": user.ilink_bot_id,
        "ilink_user_id": user.ilink_user_id,
        "base_url": user.base_url,
        "timezone": user.timezone,
        "send_at": user.send_at,
        "report_mode": user.report_mode,
        "model": None if user.model is None else {
            "base_url": user.model.base_url,
            "model": user.model.model,
            "api_key": user.model.api_key,
            "timeout": user.model.timeout,
            "max_tokens": user.model.max_tokens,
            "allow_http": user.model.allow_http,
        },
        "enabled": user.enabled,
    }


def user_to_dict(user: User) -> dict:
    """Full serialized form including secrets; for merge-on-update only, never sent to clients."""
    return _serialize(user)


class UserStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def _connect(self):
        # One short-lived connection per operation: safe across the threads of
        # the ThreadingHTTPServer that the onboarding web app uses.
        return sqlite3.connect(self.path, timeout=10)

    def _init(self) -> None:
        db = self._connect()
        try:
            db.execute("CREATE TABLE IF NOT EXISTS users ("
                       "athlete_id TEXT PRIMARY KEY, data TEXT NOT NULL, "
                       "manage_salt TEXT, manage_hash TEXT, updated TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS ilink_state ("
                       "athlete_id TEXT PRIMARY KEY, context_token TEXT, cursor TEXT, updated TEXT NOT NULL)")
            db.commit()
        finally:
            db.close()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def upsert(self, user: User) -> None:
        db = self._connect()
        try:
            with db:
                # Upsert preserves the manage_secret columns on update.
                db.execute(
                    "INSERT INTO users (athlete_id, data, updated) VALUES (?, ?, ?) "
                    "ON CONFLICT(athlete_id) DO UPDATE SET data=excluded.data, updated=excluded.updated",
                    (user.athlete_id, json.dumps(user_to_dict(user), ensure_ascii=False), self._now()),
                )
        finally:
            db.close()

    def get(self, athlete_id: Any) -> User | None:
        db = self._connect()
        try:
            row = db.execute("SELECT data FROM users WHERE athlete_id = ?",
                             (canonical_athlete(athlete_id),)).fetchone()
        finally:
            db.close()
        return make_user(json.loads(row[0])) if row else None

    def all(self) -> list[User]:
        db = self._connect()
        try:
            rows = db.execute("SELECT data FROM users ORDER BY athlete_id").fetchall()
        finally:
            db.close()
        return [make_user(json.loads(row[0])) for row in rows]

    def remove(self, athlete_id: Any) -> bool:
        db = self._connect()
        try:
            with db:
                cur = db.execute("DELETE FROM users WHERE athlete_id = ?",
                                 (canonical_athlete(athlete_id),))
            return cur.rowcount > 0
        finally:
            db.close()

    def set_enabled(self, athlete_id: Any, enabled: bool) -> User:
        user = self.get(athlete_id)
        if user is None:
            raise AppError(f"未找到用户 {canonical_athlete(athlete_id)}。")
        updated = replace(user, enabled=enabled)
        self.upsert(updated)
        return updated

    def set_manage_secret(self, athlete_id: Any, secret: str) -> None:
        salt, digest = hash_secret(secret)
        db = self._connect()
        try:
            with db:
                db.execute("UPDATE users SET manage_salt=?, manage_hash=? WHERE athlete_id=?",
                           (salt, digest, canonical_athlete(athlete_id)))
        finally:
            db.close()

    def check_manage_secret(self, athlete_id: Any, secret: str) -> bool:
        if not secret:
            return False
        db = self._connect()
        try:
            row = db.execute("SELECT manage_salt, manage_hash FROM users WHERE athlete_id=?",
                             (canonical_athlete(athlete_id),)).fetchone()
        finally:
            db.close()
        if not row or not row[0] or not row[1]:
            return False
        return verify_secret(secret, row[0], row[1])

    def has_manage_secret(self, athlete_id: Any) -> bool:
        db = self._connect()
        try:
            row = db.execute("SELECT manage_salt FROM users WHERE athlete_id=?",
                             (canonical_athlete(athlete_id),)).fetchone()
        finally:
            db.close()
        return bool(row and row[0])

    def set_ilink_state(self, athlete_id: Any, context_token: str | None = None,
                        cursor: str | None = None) -> None:
        db = self._connect()
        try:
            with db:
                db.execute(
                    "INSERT INTO ilink_state (athlete_id, context_token, cursor, updated) "
                    "VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(athlete_id) DO UPDATE SET "
                    "context_token=COALESCE(excluded.context_token, ilink_state.context_token), "
                    "cursor=COALESCE(excluded.cursor, ilink_state.cursor), updated=excluded.updated",
                    (canonical_athlete(athlete_id), context_token, cursor, self._now()),
                )
        finally:
            db.close()

    def get_ilink_state(self, athlete_id: Any) -> dict:
        db = self._connect()
        try:
            row = db.execute("SELECT context_token, cursor FROM ilink_state WHERE athlete_id=?",
                             (canonical_athlete(athlete_id),)).fetchone()
        finally:
            db.close()
        if row is None:
            return {"context_token": "", "cursor": ""}
        return {"context_token": row[0] or "", "cursor": row[1] or ""}

    def close(self) -> None:
        pass
