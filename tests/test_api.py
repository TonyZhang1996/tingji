from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import wave

from meeting_tool.deepseek_api import ApiError, DeepSeekASR


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.wav = Path(self.temp.name) / "speech.wav"
        with wave.open(str(self.wav), "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(16000)
            writer.writeframes(b"\0\0" * 1600)
        self.requests = []
        self.responses = []
        case = self
        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            def log_message(self, *_args): pass
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                case.requests.append({"path": self.path, "headers": self.headers, "body": body})
                response = case.responses.pop(0)
                if response == "truncated":
                    self.send_response(200)
                    self.send_header("Transfer-Encoding", "chunked")
                    self.end_headers()
                    self.wfile.write(b"20\r\n{\"text\":\"unfinished")
                    self.wfile.flush()
                    self.close_connection = True
                    return
                status, data = response
                payload = json.dumps(data).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1/tools/asr"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def client(self, **kwargs):
        return DeepSeekASR("only-mock-key", endpoint=self.url, timeout=2, **kwargs)

    def test_multipart_request_and_unicode_response(self):
        self.responses = [(200, {"text": "中文原话", "duration_ms": 100, "request_id": "mock-id"})]
        result = self.client().transcribe(self.wav)
        self.assertEqual(result["text"], "中文原话")
        sent = self.requests[0]
        self.assertEqual(sent["path"], "/v1/tools/asr")
        self.assertEqual(sent["headers"]["Authorization"], "Bearer only-mock-key")
        self.assertIn(b'name="file"; filename="speech.wav"', sent["body"])
        self.assertNotIn(b"only-mock-key", sent["body"])

    def test_401_and_402_are_not_retried(self):
        for status in (401, 402):
            self.responses = [(status, {"error": "rejected"})]
            before = len(self.requests)
            with self.assertRaises(ApiError) as captured:
                self.client().transcribe(self.wav)
            self.assertEqual(captured.exception.status, status)
            self.assertFalse(captured.exception.retryable)
            self.assertEqual(len(self.requests) - before, 1)

    def test_truncated_response_is_ambiguous_and_not_retried(self):
        self.responses = ["truncated"]
        with self.assertRaises(ApiError) as captured:
            self.client().transcribe(self.wav)
        self.assertTrue(captured.exception.ambiguous)
        self.assertEqual(len(self.requests), 1)

    def test_504_not_retried_due_possible_charge(self):
        self.responses = [(504, {"error": "timeout"})]
        with self.assertRaises(ApiError) as captured:
            self.client().transcribe(self.wav)
        self.assertTrue(captured.exception.ambiguous)
        self.assertEqual(len(self.requests), 1)

    def test_503_retries_explicit_error_then_succeeds(self):
        self.responses = [(503, {"error": "busy"}), (200, {"text": "恢复", "duration_ms": 100})]
        with patch("threading.Event.wait", return_value=False):
            result = self.client().transcribe(self.wav)
        self.assertEqual(result["text"], "恢复")
        self.assertEqual(len(self.requests), 2)

    def test_cancel_before_send_does_not_contact_server(self):
        event = threading.Event()
        event.set()
        with self.assertRaises(RuntimeError):
            self.client().transcribe(self.wav, event)
        self.assertEqual(self.requests, [])


if __name__ == "__main__":
    unittest.main()
