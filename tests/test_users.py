import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from mcp4intervals.core import AppError, Config, run_report
from mcp4intervals.users import (UserStore, canonical_athlete, make_user, users_from_json)


DAY = date(2026, 10, 30)


class UserParsingTests(unittest.TestCase):
    def test_make_user_normalizes_and_validates(self):
        user = make_user({
            "athlete_id": "123",
            "intervals_api_key": "secret-key",
            "bot_token": "bot-token",
            "ilink_user_id": "owner@im.wechat",
            "timezone": "Asia/Shanghai",
            "send_at": "08:30",
            "report_mode": "ai",
            "model": {"base_url": "https://model.example/v1", "model": "m", "api_key": "k"},
        })
        self.assertEqual("i123", user.athlete_id)
        self.assertEqual("Asia/Shanghai", user.timezone)
        self.assertEqual("08:30", user.send_at)
        self.assertEqual("m", user.model.model)
        self.assertTrue(user.enabled)
        self.assertTrue(user.wechat_bound)

    def test_invalid_fields_rejected(self):
        bad = [
            {"athlete_id": "abc", "intervals_api_key": "k"},
            {"athlete_id": "i1", "intervals_api_key": "k", "send_at": "25:00"},
            {"athlete_id": "i1", "intervals_api_key": "k", "timezone": "Not/AZone"},
            {"athlete_id": "i1", "intervals_api_key": "k", "model": {"base_url": "ftp://x", "model": "m"}},
        ]
        for data in bad:
            with self.subTest(data=data):
                with self.assertRaises(AppError):
                    make_user(data)

    def test_users_from_json_shapes(self):
        one = users_from_json({"athlete_id": "i1", "intervals_api_key": "k", "bot_token": "t", "ilink_user_id": "u"})
        self.assertEqual(1, len(one))
        many = users_from_json({"users": [
            {"athlete_id": "i1", "intervals_api_key": "k", "bot_token": "t", "ilink_user_id": "u"},
            {"athlete_id": "i2", "intervals_api_key": "k", "bot_token": "t2", "ilink_user_id": "u2"},
        ]})
        self.assertEqual(2, len(many))
        self.assertEqual("i2", many[1].athlete_id)

    def test_canonical_athlete(self):
        self.assertEqual("i123", canonical_athlete("123"))
        self.assertEqual("i123", canonical_athlete("i123"))
        self.assertEqual("i123", canonical_athlete("I123"))


class UserStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = UserStore(Path(self.tmp.name) / "users.sqlite3")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def user(self, athlete="i1"):
        return make_user({"athlete_id": athlete, "intervals_api_key": f"key-{athlete}",
                          "bot_token": "bot-token", "ilink_user_id": "owner@im.wechat"})

    def test_roundtrip_and_isolation(self):
        self.store.upsert(self.user("i1"))
        self.store.upsert(self.user("i2"))
        self.assertEqual(["i1", "i2"], [u.athlete_id for u in self.store.all()])
        self.assertEqual("key-i2", self.store.get("i2").intervals_api_key)
        self.assertIsNone(self.store.get("i9"))

    def test_remove_and_enable(self):
        self.store.upsert(self.user("i1"))
        self.assertTrue(self.store.remove("i1"))
        self.assertFalse(self.store.remove("i1"))
        self.store.upsert(self.user("i1"))
        self.store.set_enabled("i1", False)
        self.assertFalse(self.store.get("i1").enabled)
        self.store.set_enabled("i1", True)
        self.assertTrue(self.store.get("i1").enabled)

    def test_public_summary_hides_secrets(self):
        self.store.upsert(self.user("i1"))
        summary = self.store.get("i1").public_summary()
        text = str(summary)
        self.assertNotIn("key-i1", text)
        self.assertNotIn("bot-token", text)


class RunReportTests(unittest.TestCase):
    def test_run_report_uses_per_user_config_and_delivers(self):
        cfg = Config(api_key="user-key", athlete_id="i123", ilink_user_id="owner@im.wechat",
                     state_dir=Path(tempfile.mkdtemp()))
        snapshot = {"date": str(DAY), "wellness": [], "activities": [], "events": []}
        with patch("mcp4intervals.core.fetch_snapshot", return_value=snapshot) as fetch, \
                patch("mcp4intervals.core.deliver", return_value={"status": "accepted_by_ilink", "date": str(DAY)}) as deliver:
            result = run_report(cfg, DAY)
        fetch.assert_called_once_with(cfg, DAY)
        deliver.assert_called_once()
        self.assertEqual("accepted_by_ilink", result["status"])

    def test_run_report_skips_when_already_sent(self):
        cfg = Config(api_key="k", athlete_id="i123", ilink_user_id="owner@im.wechat",
                     state_dir=Path(tempfile.mkdtemp()))
        with patch("mcp4intervals.core.delivery_status", return_value={"status": "already_sent", "date": str(DAY)}), \
                patch("mcp4intervals.core.fetch_snapshot") as fetch:
            result = run_report(cfg, DAY)
        fetch.assert_not_called()
        self.assertEqual("already_sent", result["status"])


class ManageSecretTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = UserStore(Path(self.tmp.name) / "users.sqlite3")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def user(self, athlete="i1"):
        return make_user({"athlete_id": athlete, "intervals_api_key": f"key-{athlete}",
                          "bot_token": "bot-token", "ilink_user_id": "owner@im.wechat"})

    def test_set_check_and_upsert_preserves(self):
        self.store.upsert(self.user("i1"))
        self.assertFalse(self.store.has_manage_secret("i1"))
        self.store.set_manage_secret("i1", "correct-horse")
        self.assertTrue(self.store.has_manage_secret("i1"))
        self.assertTrue(self.store.check_manage_secret("i1", "correct-horse"))
        self.assertFalse(self.store.check_manage_secret("i1", "wrong"))
        self.assertFalse(self.store.check_manage_secret("i1", ""))
        self.store.upsert(self.user("i1"))
        self.assertTrue(self.store.check_manage_secret("i1", "correct-horse"))


if __name__ == "__main__":
    unittest.main()
