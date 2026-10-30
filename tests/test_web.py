import json
import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from mcp4intervals.users import UserStore, make_user
from mcp4intervals.web import make_handler


def _request(port, method, path, body=None, headers=None):
    data = None
    h = dict(headers or {})
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        h["Content-Type"] = "application/json"
    req = Request(f"http://127.0.0.1:{port}{path}", data=data, headers=h, method=method)
    try:
        with urlopen(req, timeout=5) as resp:
            return resp.status, resp.read().decode("utf-8")
    except HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


BASE = {"athlete_id": "i123", "intervals_api_key": "super-secret-ik", "bot_token": "bot-token",
        "ilink_user_id": "owner@im.wechat", "timezone": "Asia/Shanghai", "send_at": "08:00",
        "report_mode": "summary"}
ADMIN = {"X-Onboard-Token": "sekret-token"}


class WebTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = UserStore(Path(self.tmp.name) / "users.sqlite3")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.store))
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.store.close()
        self.tmp.cleanup()

    def create(self, secret="mysecret1"):
        return _request(self.port, "POST", "/api/user", {**BASE, "manage_secret": secret})

    def test_healthz(self):
        status, body = _request(self.port, "GET", "/healthz")
        self.assertEqual(200, status)
        self.assertEqual({"ok": True}, json.loads(body))

    def test_root_is_open_self_service(self):
        status, body = _request(self.port, "GET", "/")
        self.assertEqual(200, status)
        self.assertIn("开通", body)
        self.assertNotIn("已开通用户", body)

    def test_root_is_login_page(self):
        status, body = _request(self.port, "GET", "/")
        self.assertEqual(200, status)
        self.assertIn("登录", body)
        self.assertIn("去开通", body)

    def test_register_contains_intervals_guide(self):
        status, body = _request(self.port, "GET", "/register")
        self.assertEqual(200, status)
        self.assertIn("准备 Intervals 凭据", body)
        self.assertIn("static/guide-intervals.png", body)
        self.assertIn("static/guide-deepseek.png", body)
        self.assertIn("https://intervals.icu/settings", body)

    def test_dashboard_page(self):
        status, body = _request(self.port, "GET", "/dashboard")
        self.assertEqual(200, status)
        self.assertIn("我的配置", body)
        self.assertIn("编辑设置", body)

    def test_static_guide_images_served(self):
        for name in ("guide-intervals.png", "guide-deepseek.png"):
            req = Request(f"http://127.0.0.1:{self.port}/static/{name}", method="GET")
            with urlopen(req, timeout=5) as resp:
                self.assertEqual(200, resp.status)
                self.assertEqual("image/png", resp.headers.get("Content-Type"))
                data = resp.read(8)
                self.assertEqual(0x89, data[0])
                self.assertEqual(b"PNG", data[1:4])
        status, _ = _request(self.port, "GET", "/static/not-there.png")
        self.assertEqual(404, status)

    def test_create_requires_manage_secret(self):
        status, body = _request(self.port, "POST", "/api/user", BASE)
        self.assertEqual(400, status)
        self.assertIn("管理密码", json.loads(body)["message"])
        status, _ = _request(self.port, "POST", "/api/user", {**BASE, "manage_secret": "12345"})
        self.assertEqual(400, status)
        status, body = self.create()
        self.assertEqual(200, status)
        self.assertEqual("i123", json.loads(body)["athlete_id"])

    def test_registration_cap(self):
        with patch.dict(os.environ, {"MAX_USERS": "2"}):
            self.store.upsert(make_user({**BASE, "athlete_id": "i1"}))
            # one slot left under the cap of 2: a new registration succeeds
            status, _ = _request(self.port, "POST", "/api/user",
                                 {**BASE, "athlete_id": "i2", "manage_secret": "mysecret1"})
            self.assertEqual(200, status)
            # now at the cap: a further new registration is rejected
            status, body = _request(self.port, "POST", "/api/user",
                                    {**BASE, "athlete_id": "i3", "manage_secret": "mysecret2"})
            self.assertEqual(400, status)
            self.assertIn("上限", json.loads(body)["message"])
            self.assertEqual(2, self.store.count())

    def test_list_requires_admin(self):
        self.create()
        status, _ = _request(self.port, "GET", "/api/users")
        self.assertEqual(401, status)
        with patch.dict(os.environ, {"ONBOARD_ADMIN_TOKEN": "sekret-token"}):
            status, body = _request(self.port, "GET", "/api/users", headers=ADMIN)
            self.assertEqual(200, status)
            users = json.loads(body)["users"]
            self.assertEqual(1, len(users))
            self.assertNotIn("super-secret-ik", body)
            self.assertNotIn("bot-token", body)

    def test_update_requires_matching_secret(self):
        self.create()
        status, _ = _request(self.port, "POST", "/api/user",
                             {"athlete_id": "i123", "timezone": "Europe/Amsterdam", "manage_secret": "wrong"})
        self.assertEqual(400, status)
        status, _ = _request(self.port, "POST", "/api/user",
                             {"athlete_id": "i123", "intervals_api_key": "",
                              "timezone": "Europe/Amsterdam", "manage_secret": "mysecret1"})
        self.assertEqual(200, status)
        stored = self.store.get("i123")
        self.assertEqual("super-secret-ik", stored.intervals_api_key)
        self.assertEqual("Europe/Amsterdam", stored.timezone)

    def test_toggle_delete_and_load_require_secret(self):
        self.create()
        status, _ = _request(self.port, "POST", "/api/user/i123/toggle", {"manage_secret": "wrong"})
        self.assertEqual(400, status)
        status, body = _request(self.port, "POST", "/api/user/i123/load", {"manage_secret": "mysecret1"})
        self.assertEqual(200, status)
        self.assertEqual("i123", json.loads(body)["athlete_id"])
        status, body = _request(self.port, "POST", "/api/user/i123/toggle", {"manage_secret": "mysecret1"})
        self.assertEqual(200, status)
        self.assertFalse(json.loads(body)["enabled"])
        status, _ = _request(self.port, "POST", "/api/user/i123/delete", {"manage_secret": "mysecret1"})
        self.assertEqual(200, status)
        self.assertEqual(0, len(self.store.all()))

    def test_admin_override_without_manage_secret(self):
        self.create()
        with patch.dict(os.environ, {"ONBOARD_ADMIN_TOKEN": "sekret-token"}):
            status, body = _request(self.port, "POST", "/api/user/i123/load", {}, headers=ADMIN)
            self.assertEqual(200, status)
            status, body = _request(self.port, "POST", "/api/user/i123/toggle", {}, headers=ADMIN)
            self.assertEqual(200, status)
            self.assertFalse(json.loads(body)["enabled"])
            status, _ = _request(self.port, "POST", "/api/user/i123/delete", {}, headers=ADMIN)
            self.assertEqual(200, status)
            self.assertEqual(0, len(self.store.all()))

    def test_admin_page_gated(self):
        with patch.dict(os.environ, {"ONBOARD_ADMIN_TOKEN": "sekret-token"}):
            status, body = _request(self.port, "GET", "/admin")
            self.assertEqual(200, status)
            self.assertIn("管理员令牌", body)
            status, body = _request(self.port, "GET", "/admin", headers=ADMIN)
            self.assertEqual(200, status)
            self.assertIn("已开通用户", body)


if __name__ == "__main__":
    unittest.main()
