import contextlib
import io
import json
import os
import tempfile
import unittest
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from unittest.mock import patch

from mcp4intervals.__main__ import main
from mcp4intervals.ai import ModelConfig, ModelError, analyze_snapshot
from mcp4intervals.core import AppError, Config, deliver, delivery_status


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
        with patch.dict(os.environ, {"REPORT_MODE": "ai"}, clear=True), patch.object(Config, "today", return_value=DAY), patch("mcp4intervals.__main__.delivery_status", return_value=None), patch("mcp4intervals.__main__.fetch_snapshot", return_value=SNAPSHOT), patch("mcp4intervals.ai.analyze_snapshot", return_value="AI报告") as model, patch("mcp4intervals.__main__.deliver", return_value={"status": "accepted_by_serverchan"}) as push:
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
            cfg = Config(sendkey="SCTexample", state_dir=Path(tmp))
            self.assertIsNone(delivery_status(cfg, DAY))
            self.assertFalse((Path(tmp) / "deliveries.sqlite3").exists())
            with self.assertRaises(AppError):
                deliver(cfg, DAY, "title", "report", sender=lambda *args: (_ for _ in ()).throw(TimeoutError()))
            with self.assertRaises(AppError):
                delivery_status(cfg, DAY)


if __name__ == "__main__":
    unittest.main()
