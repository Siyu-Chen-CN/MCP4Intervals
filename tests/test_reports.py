import io
import tempfile
import unittest
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

from mcp4intervals.core import (AppError, Config, deliver, render_report,
                                request_json, scheduled_tick, send_serverchan)


class ReportTests(unittest.TestCase):
    def test_missing_today_never_reuses_yesterdays_wellness(self):
        day = date(2026, 10, 30)
        report = render_report({"date": str(day), "wellness": [
            {"id": "2026-10-29", "sleepSecs": 28800, "hrv": 99}
        ]}, day)
        self.assertIn("睡眠：未知", report)
        self.assertIn("HRV：未知", report)
        self.assertNotIn("8小时0分", report)

    def test_load_excludes_today_and_old_records_keeps_missing_unknown(self):
        day = date(2026, 10, 30)
        report = render_report({"date": str(day), "activities": [
            {"start_date_local": "2026-10-22T08:00:00", "icu_training_load": 999},
            {"start_date_local": "2026-10-23T08:00:00", "icu_training_load": 0},
            {"start_date_local": "2026-10-29T08:00:00", "icu_training_load": None},
            {"start_date_local": "2026-10-30T08:00:00", "icu_training_load": 888},
        ]}, day)
        self.assertIn("2次活动，已知训练负荷合计 0", report)
        self.assertIn("1 次负荷缺失", report)
        self.assertNotIn("999", report)

    def test_rejects_stale_snapshot(self):
        with self.assertRaises(AppError):
            render_report({"date": "2026-10-29"}, date(2026, 10, 30))


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(sendkey="SCTexample", state_dir=Path(self.tmp.name))
        self.day = date(2026, 10, 30)

    def tearDown(self):
        self.tmp.cleanup()

    def test_sequential_and_overlapping_process_slots_send_once(self):
        sent = []
        def run(_):
            return deliver(self.cfg, self.day, "title", "body", sender=lambda *a: sent.append(a))
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(run, range(4)))
        self.assertEqual(1, len(sent))
        self.assertEqual(1, sum(r["status"] == "accepted_by_serverchan" for r in results))
        self.assertEqual(3, sum(r["status"] == "already_sent" for r in results))

    def test_ambiguous_failure_is_not_retried(self):
        calls = []
        def fail(*args):
            calls.append(args)
            raise RuntimeError("SCTsecret-must-not-leak")
        with self.assertRaises(AppError) as first:
            deliver(self.cfg, self.day, "title", "body", sender=fail)
        with self.assertRaises(AppError):
            deliver(self.cfg, self.day, "title", "body", sender=fail)
        self.assertEqual(1, len(calls))
        self.assertNotIn("SCTsecret", str(first.exception))

    def test_empty_and_oversize_reports_are_rejected_before_sending(self):
        sent = []
        for body in ("", "中" * 10_000):
            with self.assertRaises(AppError):
                deliver(self.cfg, self.day, "title", body, sender=lambda *a: sent.append(a))
        self.assertEqual([], sent)

    def test_provider_code_is_required_and_payload_is_encoded(self):
        with patch("mcp4intervals.core.request_json", return_value={"code": 0}) as req:
            send_serverchan(self.cfg, "测试", "完整中文报告")
            self.assertIn(b"desp=", req.call_args.kwargs["data"])
        for response in ({"code": 40001}, {"code": "0"}, {}, {"code": False}):
            with patch("mcp4intervals.core.request_json", return_value=response):
                with self.assertRaises(AppError):
                    send_serverchan(self.cfg, "测试", "报告")

    def test_http_exception_hides_url_with_sendkey(self):
        error = urllib.error.HTTPError("https://example.test/SCTsecret.send", 403, "secret", {}, io.BytesIO())
        with patch("urllib.request.build_opener") as build:
            build.return_value.open.side_effect = error
            with self.assertRaises(AppError) as raised:
                request_json("https://example.test/SCTsecret.send")
        self.assertNotIn("SCTsecret", str(raised.exception))
        self.assertIn("403", str(raised.exception))


class ScheduleTests(unittest.TestCase):
    def test_amsterdam_seven_am_tracks_winter_and_summer(self):
        cfg = Config()
        for stamp in ("2026-07-01T05:00:00+00:00", "2026-10-30T06:00:00+00:00"):
            now = datetime.fromisoformat(stamp)
            sent = []
            self.assertTrue(scheduled_tick(cfg, now, sent.append))
            self.assertEqual(1, len(sent))

    def test_before_seven_am_no_send_and_restart_catches_up(self):
        cfg = Config()
        sent = []
        self.assertFalse(scheduled_tick(cfg, datetime(2026, 10, 30, 5, 59, tzinfo=timezone.utc), sent.append))
        self.assertTrue(scheduled_tick(cfg, datetime(2026, 10, 30, 8, 30, tzinfo=timezone.utc), sent.append))
        self.assertEqual([date(2026, 10, 30)], sent)

    def test_naive_datetime_rejected(self):
        with self.assertRaises(AppError):
            scheduled_tick(Config(), datetime(2026, 10, 30), lambda day: None)


if __name__ == "__main__":
    unittest.main()
