import contextlib
import io
import json
import os
import tempfile
import unittest
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from unittest.mock import patch

from mcp4intervals.__main__ import main
from mcp4intervals.ai import ModelConfig, ModelError, _parse_workout, analyze_snapshot, chat_reply
from mcp4intervals.core import AppError, Config, build_ai_coach_event, create_event, deliver, delivery_status


DAY = date(2026, 10, 30)
SNAPSHOT = {"date": str(DAY), "wellness": [{"id": str(DAY), "restingHR": 49, "secret": "not-for-model"}]}


class AnalysisTests(unittest.TestCase):
    def setUp(self):
        self.cfg = ModelConfig("https://model.example/v1", "user-model", "model-secret")

    def test_custom_endpoint_model_auth_and_minimal_summary(self):
        response = {"choices": [{"message": {"content": "今天建议轻松骑。"}, "finish_reason": "stop"}]}
        with patch("mcp4intervals.ai.request_json", return_value=response) as request:
            report = analyze_snapshot(SNAPSHOT, DAY, self.cfg)
        args, kwargs = request.call_args
        payload = json.loads(kwargs["data"])
        self.assertEqual("https://model.example/v1/chat/completions", args[0])
        self.assertEqual("user-model", payload["model"])
        self.assertEqual("Bearer model-secret", kwargs["headers"]["Authorization"])
        self.assertEqual(120, kwargs["timeout"])
        self.assertIn("今天建议轻松骑", report)
        self.assertNotIn("not-for-model", json.dumps(payload))
        self.assertNotIn("model-secret", json.dumps(payload))
        self.assertNotIn("model-secret", repr(self.cfg))

    def test_endpoint_normalization_and_http_policy(self):
        for base in ("https://model.example/v1/", "https://model.example/v1/chat/completions"):
            self.assertEqual("https://model.example/v1/chat/completions", ModelConfig(base, "x").endpoint)
        ModelConfig("http://localhost:11434/v1", "x").validate()
        ModelConfig("http://lan-model:11434/v1", "x", allow_http=True).validate()
        for base in ("", "ftp://model.example", "https://user:secret@model.example/v1", "https://model.example?key=secret", "http://lan-model/v1"):
            with self.assertRaises(AppError):
                ModelConfig(base, "x").validate()

    def test_invalid_and_incomplete_responses_are_not_used(self):
        responses = [{}, [], {"choices": []}, {"choices": [None]},
                     {"choices": [{"message": {"content": ""}}]},
                     {"choices": [{"message": {"content": "partial"}, "finish_reason": "length"}]},
                     {"choices": [{"message": {"content": "中" * 10000}}]}]
        for response in responses:
            with self.subTest(response_type=type(response).__name__):
                with patch("mcp4intervals.ai.request_json", return_value=response):
                    with self.assertRaises(AppError):
                        analyze_snapshot(SNAPSHOT, DAY, self.cfg)

    def test_stale_snapshot_does_not_call_model(self):
        with patch("mcp4intervals.ai.request_json") as request:
            with self.assertRaises(AppError):
                analyze_snapshot({"date": "2026-10-29"}, DAY, self.cfg)
        request.assert_not_called()

    def test_provider_error_is_sanitized(self):
        with patch("mcp4intervals.ai.request_json", side_effect=AppError("model-secret")):
            with self.assertRaises(AppError) as error:
                analyze_snapshot(SNAPSHOT, DAY, self.cfg)
        self.assertNotIn("model-secret", str(error.exception))

    def test_local_http_provider_roundtrip(self):
        captured = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                captured.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"choices": [{"message": {"content": "本地模型报告"}, "finish_reason": "stop"}]}).encode())

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            cfg = ModelConfig(f"http://127.0.0.1:{server.server_port}/v1", "local-model")
            self.assertIn("本地模型报告", analyze_snapshot(SNAPSHOT, DAY, cfg))
            self.assertEqual("/v1/chat/completions", captured[0][0])
            self.assertEqual("local-model", captured[0][1]["model"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_parse_workout_marker(self):
        text, workout = _parse_workout('建议轻松骑。\n<<<WORKOUT>>>\n{"workout": {"name": "恢复骑", "date": "2026-10-31"}}\n<<<END>>>')
        self.assertEqual("建议轻松骑。", text)
        self.assertEqual("恢复骑", workout["name"])

    def test_parse_workout_no_marker(self):
        text, workout = _parse_workout("没有课程，只是建议。")
        self.assertEqual("没有课程，只是建议。", text)
        self.assertIsNone(workout)

    def test_parse_workout_structured(self):
        content = ('建议。\n<<<WORKOUT>>>\nname: 恢复骑\ndate: 2026-10-31\ntype: Ride\nmoving_time: 3660\n---\n'
                   'Warmup\n- Easy start 15m ramp 45%-65% ftp\n\n2x\n- Steady tempo 12m 80%-85% ftp\n- Recover 4m 50% ftp\n<<<END>>>')
        text, workout = _parse_workout(content)
        self.assertEqual("建议。", text)
        self.assertEqual("恢复骑", workout["name"])
        self.assertEqual(3660, workout["moving_time"])
        self.assertEqual("Ride", workout["type"])
        self.assertIn("- Easy start 15m ramp 45%-65% ftp", workout["description"])
        self.assertIn("2x", workout["description"])

    def test_chat_reply_returns_answer_and_workout(self):
        content = '今天建议休息。\n<<<WORKOUT>>>\n{"workout": {"name": "轻松骑", "date": "2026-10-31", "moving_time": 3600}}\n<<<END>>>'
        response = {"choices": [{"message": {"content": content}, "finish_reason": "stop"}]}
        with patch("mcp4intervals.ai.request_json", return_value=response):
            answer, workout = chat_reply(SNAPSHOT, "安排明天", DAY, self.cfg)
        self.assertIn("今天建议休息", answer)
        self.assertEqual("轻松骑", workout["name"])

    def test_build_ai_coach_event(self):
        event = build_ai_coach_event({"name": "恢复骑", "date": "2026-10-31", "moving_time": 3600, "description": "Z2"}, DAY)
        self.assertTrue(event["name"].startswith("AI Coach"))
        self.assertEqual("2026-10-31T00:00:00", event["start_date_local"])
        self.assertEqual("WORKOUT", event["category"])
        self.assertEqual(3600, event["moving_time"])
        event2 = build_ai_coach_event({"name": "x", "date": "bad"}, DAY)
        self.assertEqual((DAY + timedelta(days=1)).isoformat() + "T00:00:00", event2["start_date_local"])

    def test_create_event_posts_payload(self):
        cfg = Config(api_key="ik", athlete_id="i1")
        with patch("mcp4intervals.core.request_json", return_value={"id": 123}) as req:
            result = create_event(cfg, {"name": "AI Coach: x"})
        self.assertEqual({"id": 123}, result)
        args, kwargs = req.call_args
        self.assertEqual("https://intervals.icu/api/v1/athlete/i1/events", args[0])
        payload = json.loads(kwargs["data"])
        self.assertEqual("AI Coach: x", payload["name"])


class WorkflowTests(unittest.TestCase):
    def invoke(self, args):
        with patch("sys.argv", ["mcp4intervals", "--env", "missing-test.env", *args]), contextlib.redirect_stdout(io.StringIO()):
            return main()

    def test_preview_ai_never_sends_wechat(self):
        with patch.dict(os.environ, {}, clear=True), patch("mcp4intervals.ai.analyze_snapshot", return_value="AI报告") as model, patch("mcp4intervals.__main__.deliver") as push:
            code = self.invoke(["preview", "--mode", "ai", "--fixture", "examples/snapshot.json", "--date", str(DAY)])
        self.assertEqual(0, code)
        model.assert_called_once()
        push.assert_not_called()

    def test_report_mode_env_and_daily_delivery(self):
        with patch.dict(os.environ, {"REPORT_MODE": "ai"}, clear=True), patch.object(Config, "today", return_value=DAY), patch("mcp4intervals.__main__.delivery_status", return_value=None), patch("mcp4intervals.__main__.fetch_snapshot", return_value=SNAPSHOT), patch("mcp4intervals.ai.analyze_snapshot", return_value="AI报告") as model, patch("mcp4intervals.__main__.deliver", return_value={"status": "accepted_by_ilink"}) as push:
            self.assertEqual(0, self.invoke(["send"]))
        model.assert_called_once()
        self.assertEqual("AI报告", push.call_args.args[3])

    def test_already_sent_skips_fetch_and_model(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(Config, "today", return_value=DAY), patch("mcp4intervals.__main__.delivery_status", return_value={"status": "already_sent"}), patch("mcp4intervals.__main__.fetch_snapshot") as fetch, patch("mcp4intervals.ai.analyze_snapshot") as model:
            self.assertEqual(0, self.invoke(["send", "--mode", "ai"]))
        fetch.assert_not_called()
        model.assert_not_called()

    def test_daemon_does_not_repeatedly_charge_failed_model(self):
        sleeps = []

        def sleep(_):
            sleeps.append(True)
            if len(sleeps) == 2:
                raise KeyboardInterrupt

        with patch.dict(os.environ, {"REPORT_TIME": "00:00"}, clear=True), patch("mcp4intervals.__main__.delivery_status", return_value=None), patch("mcp4intervals.__main__.fetch_snapshot", return_value=SNAPSHOT), patch("mcp4intervals.ai.analyze_snapshot", side_effect=ModelError("服务暂不可用")) as model, patch("mcp4intervals.__main__.time.sleep", side_effect=sleep), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(0, self.invoke(["daemon", "--mode", "ai"]))
        model.assert_called_once()

    def test_uncertain_delivery_blocks_model_and_status_does_not_create_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config(ilink_user_id="owner@im.wechat", state_dir=Path(tmp))
            self.assertIsNone(delivery_status(cfg, DAY))
            self.assertFalse((Path(tmp) / "deliveries.sqlite3").exists())
            with self.assertRaises(AppError):
                deliver(cfg, DAY, "title", "report", sender=lambda *args: (_ for _ in ()).throw(TimeoutError()))
            with self.assertRaises(AppError):
                delivery_status(cfg, DAY)


if __name__ == "__main__":
    unittest.main()
