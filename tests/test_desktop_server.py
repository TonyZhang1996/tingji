"""Loopback HTTP and media security checks using temporary mock meetings."""
from __future__ import annotations

from http import HTTPStatus
import json
from pathlib import Path
import tempfile
import unittest
from urllib import error, parse, request

from meeting_tool.desktop_controller import MeetingController
from meeting_tool.desktop_server import DesktopServer
from meeting_tool.engine import RunConfig
from meeting_tool.exports import atomic_json
from test_desktop_controller import FakeJob, create_audio, prepare_complete


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="meeting_http_test_")
        self.root = Path(self.temp.name)
        self.dist = self.root / "web" / "dist"
        self.dist.mkdir(parents=True)
        (self.dist / "index.html").write_text("<!doctype html><html><head></head><body><div id='root'></div></body></html>", encoding="utf-8")
        self.audio = create_audio(self.root / "source.wav")
        self.output = self.root / "results"
        self.output.mkdir()
        self.settings = self.root / "settings.json"
        atomic_json(self.settings, {"output_root": str(self.output), "num_speakers": 2})
        prepare_complete(RunConfig(self.audio, self.output, num_speakers=2))
        self.controller = MeetingController(self.root, settings_path=self.settings, job_factory=FakeJob)
        self.controller.dispatch("select_job", {"id": self.controller.state()["jobs"][0]["id"]})
        self.server = DesktopServer(self.controller, self.dist, port=0).start()
        self.base = self.server.url.rstrip("/")
        self.origin = f"http://127.0.0.1:{self.server.port}"
        with request.urlopen(self.base + "/") as response:
            self.index_body = response.read()
            self.cookie = response.headers["Set-Cookie"].split(";", 1)[0]
            self.cookie_header = response.headers["Set-Cookie"]

    def tearDown(self):
        self.server.shutdown()
        self.temp.cleanup()

    def call(self, path, *, method="GET", payload=None, token=True, cookie=True, headers=None):
        request_headers = {}
        if token:
            request_headers["X-Meeting-Token"] = self.server.token
        if cookie:
            request_headers["Cookie"] = self.cookie
        if headers:
            request_headers.update(headers)
        data = None
        if payload is not None:
            data = json.dumps(payload).encode()
            request_headers["Content-Type"] = "application/json"
        req = request.Request(self.base + path, data=data, headers=request_headers, method=method)
        try:
            with request.urlopen(req, timeout=3) as response:
                return response.status, dict(response.headers), response.read()
        except error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read()

    def media_path(self):
        state = self.controller.state()
        selected = state["selected_job"]
        value = selected.get("audio_url") or selected["samples"][0]["audio_url"]
        return parse.urlsplit(value).path

    def test_bootstrap_has_session_token_and_strict_http_only_cookie(self):
        self.assertIn(b"window.__MEETING_TOKEN__", self.index_body)
        self.assertIn(self.server.token.encode(), self.index_body)
        self.assertIn("HttpOnly", self.cookie_header)
        self.assertIn("SameSite=Strict", self.cookie_header)

    def test_api_requires_token_even_when_session_cookie_is_present(self):
        code, _, _ = self.call("/api/state", token=False)
        self.assertIn(code, (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN))
        code, _, body = self.call("/api/state")
        self.assertEqual(code, HTTPStatus.OK)
        state = json.loads(body)
        self.assertEqual(len(state["selected_job"]["segments"]), 2)
        self.assertNotIn("api_key", state)

    def test_cross_origin_post_and_rebound_host_are_rejected(self):
        code, _, _ = self.call("/api/config", method="POST", payload={"num_speakers": 2},
                               headers={"Origin": "https://untrusted.example"})
        self.assertEqual(code, HTTPStatus.FORBIDDEN)
        code, _, _ = self.call("/api/state", headers={"Host": "untrusted.example"})
        self.assertEqual(code, HTTPStatus.FORBIDDEN)

    def test_media_requires_session_cookie_and_supports_seeking_ranges(self):
        path = self.media_path()
        code, _, _ = self.call(path, cookie=False)
        self.assertIn(code, (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN))
        code, headers, body = self.call(path, headers={"Range": "bytes=10-19"})
        self.assertEqual(code, HTTPStatus.PARTIAL_CONTENT)
        self.assertEqual(len(body), 10)
        self.assertTrue(headers["Content-Range"].startswith("bytes 10-19/"))
        self.assertEqual(headers.get("Accept-Ranges"), "bytes")
        code, headers, body = self.call(path, headers={"Range": "bytes=-16"})
        self.assertEqual(code, HTTPStatus.PARTIAL_CONTENT)
        self.assertEqual(len(body), 16)
        code, headers, body = self.call(path, method="HEAD")
        self.assertEqual(code, HTTPStatus.OK)
        self.assertEqual(body, b"")
        self.assertGreater(int(headers["Content-Length"]), 16)

    def test_unsatisfiable_and_multiple_media_ranges_are_rejected(self):
        path = self.media_path()
        for value in ("bytes=999999999-", "bytes=1-3,8-9", "bytes=20-10"):
            code, _, _ = self.call(path, headers={"Range": value})
            self.assertEqual(code, HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)

    def test_static_path_traversal_cannot_read_local_files(self):
        marker = "private-outside-dist-marker-3e10"
        (self.root / "secret.txt").write_text(marker, encoding="utf-8")
        for path in ("/%2e%2e/%2e%2e/secret.txt", "/..%5c..%5csecret.txt", "/api/media/../../secret.txt"):
            _, _, body = self.call(path)
            self.assertNotIn(marker.encode(), body)

    def test_unknown_media_id_and_invalid_job_do_not_expose_files(self):
        code, _, _ = self.call("/api/media/not-registered")
        self.assertEqual(code, HTTPStatus.NOT_FOUND)
        code, _, body = self.call("/api/select_job", method="POST", payload={"id": str(self.root)})
        self.assertIn(code, (HTTPStatus.BAD_REQUEST, HTTPStatus.NOT_FOUND))
        self.assertIn("message", json.loads(body))

    def test_delete_job_requires_authorized_request_and_removes_only_task_results(self):
        identifier = self.controller.state()["selected_job"]["id"]
        directory = self.controller._jobs[identifier]
        media = self.media_path()
        payload = {"job_id": identifier}
        for kwargs in ({"token": False}, {"headers": {"Origin": "https://untrusted.example"}}):
            code, _, _ = self.call("/api/delete_job", method="POST", payload=payload, **kwargs)
            self.assertEqual(code, HTTPStatus.FORBIDDEN)
            self.assertTrue((directory / "analysis.json").is_file())
        code, _, body = self.call("/api/delete_job", method="POST", payload=payload)
        self.assertEqual(code, HTTPStatus.OK)
        state = json.loads(body)
        self.assertEqual(state["jobs"], [])
        self.assertIsNone(state["selected_job"])
        self.assertIsNone(state["source"])
        self.assertFalse(directory.exists())
        self.assertTrue(self.audio.is_file())
        code, _, _ = self.call(media)
        self.assertEqual(code, HTTPStatus.NOT_FOUND)
        code, _, _ = self.call("/api/delete_job", method="POST", payload=payload)
        self.assertEqual(code, HTTPStatus.BAD_REQUEST)


if __name__ == "__main__":
    unittest.main()
