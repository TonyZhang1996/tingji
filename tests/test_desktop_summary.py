"""Summary orchestration regressions, using temporary meetings and a local fake.

No external API, real recording, or user credential is used by this module.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib import error, request

from meeting_tool.desktop_controller import MeetingController
from meeting_tool.desktop_server import DesktopServer
from meeting_tool.engine import RunConfig, job_directory
from meeting_tool.exports import atomic_json
from meeting_tool.summarization import PROMPT_VERSION, render_markdown, speaker_catalog
from test_desktop_controller import FakeJob, create_audio, prepare_complete, wait_idle


class FakeSummaryService:
    calls = []
    entered = threading.Event()
    release = None
    fail = False
    omit_people = False

    def __init__(self, api_key, *, log=None, **kwargs):
        self.api_key = api_key
        self.log = log or (lambda _message: None)

    def generate(self, snapshot, job_dir, cancel_event, progress):
        type(self).calls.append(copy.deepcopy(snapshot))
        type(self).entered.set()
        progress("map", 1, 2, "模拟整理发言")
        if type(self).release:
            type(self).release.wait(5)
        if type(self).fail:
            self.log("模拟错误包含Key：" + self.api_key)
            raise RuntimeError("模拟接口失败：" + self.api_key)
        # Deliberately ignore cancellation here: the controller must still
        # prevent this in-flight response from replacing an existing report.
        report = {
            "schema_version": 1,
            "prompt_version": PROMPT_VERSION,
            "model": "deepseek-flash",
            "title": snapshot["title"],
            "created_at": "2026-11-15T12:00:00+08:00",
            "input_fingerprint": snapshot["fingerprint"],
            "coverage": copy.deepcopy(snapshot["coverage"]),
            "content": {
                "overview": "模拟会议总结，保留讨论与待确认事项。",
                "speaker_summaries": [{"speaker": identifier, "speaker_name": name,
                                       "summary": "讨论数字复核。", "points": ["建议复核数字。"],
                                       "commitments": [], "evidence": "00:00:00—00:00:05"}
                                      for identifier, name in speaker_catalog(snapshot).items()],
                "topics": [{"title": "模拟议题", "summary": "整理各方讨论。"}],
                "decisions": ["会议明确需要复核数字。"],
                "open_questions": ["具体提交日期尚未明确。"],
                "todos": [{"task": "复核数字", "owner": "未明确", "deadline": "未明确",
                           "evidence": "00:00:00—00:00:05"}],
            },
            "usage": {"prompt_tokens": 12, "completion_tokens": 11, "total_tokens": 23},
        }
        if type(self).omit_people:
            report["content"]["speaker_summaries"] = []
        return report


class SummaryControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="meeting_summary_qa_")
        self.root = Path(self.temp.name)
        self.audio = create_audio(self.root / "source.wav")
        self.output = self.root / "results"
        self.output.mkdir()
        self.settings = self.root / "settings.json"
        atomic_json(self.settings, {"output_root": str(self.output), "num_speakers": 2})
        self.config = RunConfig(self.audio, self.output, num_speakers=2)
        prepare_complete(self.config)
        self.directory = job_directory(self.config)
        FakeSummaryService.calls = []
        FakeSummaryService.entered = threading.Event()
        FakeSummaryService.release = None
        FakeSummaryService.fail = False
        FakeSummaryService.omit_people = False
        self.opened = []
        self.controller = self.fresh_controller()

    def fresh_controller(self):
        return MeetingController(self.root, settings_path=self.settings,
                                 job_factory=FakeJob, summary_factory=FakeSummaryService,
                                 opener=self.opened.append)

    def tearDown(self):
        if FakeSummaryService.release:
            FakeSummaryService.release.set()
        wait_idle(self.controller)
        self.temp.cleanup()

    def summary_state(self):
        return self.controller.state()["selected_job"]["ai_summary"]

    def generate(self, **kwargs):
        payload = {"api_key": "mock-summary-key-qa-only", **kwargs}
        self.controller.dispatch("summarize", payload)
        return wait_idle(self.controller)["selected_job"]["ai_summary"]

    def make_partial(self):
        checkpoint_path = self.directory / "checkpoint.json"
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        checkpoint["segments"][1].update(state="pending", text="")
        atomic_json(checkpoint_path, checkpoint)

    def test_complete_uses_corrected_text_and_names_without_changing_checkpoint(self):
        self.controller.dispatch("speaker_names", {"names": {"S1": "甲", "S2": "乙"}})
        first = self.controller.state()["selected_job"]["segments"][0]
        self.controller.dispatch("segment_review", {
            "id": first["id"], "text": "人工校正后的讨论", "speaker": "S2", "reviewed": True,
        })
        original = (self.directory / "checkpoint.json").read_bytes()
        result = self.generate()
        self.assertEqual(result["status"], "complete")
        self.assertFalse(result["stale"])
        snapshot = FakeSummaryService.calls[-1]
        self.assertEqual(snapshot["segments"][0]["text"], "人工校正后的讨论")
        self.assertEqual(snapshot["segments"][0]["speaker_name"], "乙")
        self.assertEqual(snapshot["segments"][0]["speaker"], "S2")
        self.assertTrue(snapshot["coverage"]["full"])
        self.assertEqual(snapshot["coverage"]["done_count"], 2)
        self.assertEqual((self.directory / "checkpoint.json").read_bytes(), original)
        self.assertTrue((self.directory / "AI总结.json").is_file())
        markdown = (self.directory / "AI总结.md").read_text(encoding="utf-8-sig")
        self.assertIn("待办事项", markdown)
        self.assertIn("复核数字", markdown[markdown.rfind("待办事项"):])
        self.assertIn("## 按人物总结", markdown)
        self.assertEqual({p["speaker"] for p in result["report"]["content"]["speaker_summaries"]}, {"S2", "unassigned"})

    def test_missing_person_result_cannot_replace_an_existing_report(self):
        self.generate()
        before = {name: (self.directory / name).read_bytes() for name in ("AI总结.json", "AI总结.md")}
        FakeSummaryService.omit_people = True
        result = self.generate()
        self.assertEqual(result["status"], "error")
        self.assertIn("遗漏", result["message"])
        for name, raw in before.items():
            self.assertEqual((self.directory / name).read_bytes(), raw)

    def test_legacy_report_remains_visible_and_exportable_without_automatic_generation(self):
        result = self.generate()
        legacy = result["report"]
        legacy["content"].pop("speaker_summaries")
        legacy["prompt_version"] = "meeting-summary-v1"
        atomic_json(self.directory / "AI总结.json", legacy)
        (self.directory / "AI总结.md").write_text(render_markdown(legacy), encoding="utf-8-sig")
        before = (self.directory / "AI总结.json").read_bytes()
        calls = len(FakeSummaryService.calls)
        restored = self.fresh_controller()
        summary = restored.state()["selected_job"]["ai_summary"]
        self.assertIsNotNone(summary["report"])
        self.assertTrue(summary["needs_speaker_summary"])
        self.assertTrue(summary["stale"])
        self.assertEqual(len(FakeSummaryService.calls), calls)
        restored.dispatch("open_summary", {})
        self.assertEqual((self.directory / "AI总结.json").read_bytes(), before)
        self.assertIn("旧版总结尚未按人物整理", (self.directory / "AI总结.md").read_text(encoding="utf-8-sig"))

    def test_partial_requires_explicit_opt_in_and_reports_coverage(self):
        self.make_partial()
        with self.assertRaises(ValueError):
            self.controller.dispatch("summarize", {"api_key": "mock-only"})
        self.assertEqual(FakeSummaryService.calls, [])
        result = self.generate(allow_partial=True)
        self.assertEqual(result["status"], "complete")
        self.assertFalse(result["report"]["coverage"]["full"])
        self.assertEqual(result["report"]["coverage"]["done_count"], 1)
        self.assertEqual(result["report"]["coverage"]["total_count"], 2)

    def test_empty_transcript_is_not_sent(self):
        checkpoint_path = self.directory / "checkpoint.json"
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        for row in checkpoint["segments"]:
            row["text"] = "  "
        atomic_json(checkpoint_path, checkpoint)
        with self.assertRaises(ValueError):
            self.controller.dispatch("summarize", {"api_key": "mock-only"})
        self.assertEqual(FakeSummaryService.calls, [])

    def test_report_restores_and_becomes_stale_after_human_edit(self):
        result = self.generate()
        fingerprint = result["report"]["input_fingerprint"]
        restored = self.fresh_controller()
        restored_summary = restored.state()["selected_job"]["ai_summary"]
        self.assertEqual(restored_summary["report"]["input_fingerprint"], fingerprint)
        self.assertFalse(restored_summary["stale"])
        self.controller.dispatch("speaker_names", {"names": {"S1": "新名字"}})
        self.assertTrue(self.summary_state()["stale"])
        self.assertEqual(self.summary_state()["report"]["input_fingerprint"], fingerprint)
        self.generate()
        self.assertFalse(self.summary_state()["stale"])
        first = self.controller.state()["selected_job"]["segments"][0]
        self.controller.dispatch("segment_review", {"id": first["id"], "text": "新修订的文字"})
        self.assertTrue(self.summary_state()["stale"])

    def test_error_redacts_key_and_preserves_previous_report(self):
        self.generate()
        before = {name: (self.directory / name).read_bytes() for name in ("AI总结.json", "AI总结.md")}
        FakeSummaryService.fail = True
        secret = "mock-summary-secret-never-persist-18de"
        result = self.generate(api_key=secret)
        self.assertEqual(result["status"], "error")
        self.assertIsNotNone(result["report"])
        self.assertNotIn(secret, json.dumps(self.controller.state(), ensure_ascii=False))
        for name, raw in before.items():
            self.assertEqual((self.directory / name).read_bytes(), raw)
        for path in self.root.rglob("*"):
            if path.is_file():
                self.assertNotIn(secret.encode(), path.read_bytes(), str(path))

    def test_cancellation_keeps_busy_and_discards_late_success(self):
        self.generate()
        before = {name: (self.directory / name).read_bytes() for name in ("AI总结.json", "AI总结.md")}
        FakeSummaryService.entered = threading.Event()
        FakeSummaryService.release = threading.Event()
        self.controller.dispatch("summarize", {"api_key": "mock-only"})
        self.assertTrue(FakeSummaryService.entered.wait(2))
        cancelled = self.controller.dispatch("cancel_summary", {})
        self.assertTrue(cancelled["task"]["running"])
        self.assertTrue(cancelled["task"]["cancel_requested"])
        with self.assertRaises(ValueError):
            self.controller.dispatch("analyze", {})
        FakeSummaryService.release.set()
        state = wait_idle(self.controller)
        self.assertEqual(state["selected_job"]["ai_summary"]["status"], "cancelled")
        self.assertIsNotNone(state["selected_job"]["ai_summary"]["report"])
        for name, raw in before.items():
            self.assertEqual((self.directory / name).read_bytes(), raw)

    def test_summary_blocks_source_switch_and_transcription_and_review(self):
        FakeSummaryService.release = threading.Event()
        self.controller.dispatch("summarize", {"api_key": "mock-only"})
        self.assertTrue(FakeSummaryService.entered.wait(2))
        current = self.controller.state()
        self.assertEqual(current["task"]["stage"], "summary")
        for action, payload in [
            ("transcribe", {"api_key": "mock-only"}),
            ("select_job", {"id": current["selected_job"]["id"]}),
            ("speaker_names", {"names": {"S1": "甲"}}),
            ("config", {"max_workers": 1}),
        ]:
            with self.subTest(action=action), self.assertRaises(ValueError):
                self.controller.dispatch(action, payload)
        FakeSummaryService.release.set()
        wait_idle(self.controller)

    def test_close_waits_for_inflight_summary_and_discards_result(self):
        FakeSummaryService.release = threading.Event()
        self.controller.dispatch("summarize", {"api_key": "mock-only"})
        self.assertTrue(FakeSummaryService.entered.wait(2))
        closer = threading.Thread(target=self.controller.close)
        closer.start()
        deadline = time.monotonic() + 2
        while not self.controller.state()["task"]["cancel_requested"] and time.monotonic() < deadline:
            threading.Event().wait(0.01)
        self.assertTrue(self.controller.state()["task"]["cancel_requested"])
        self.assertTrue(closer.is_alive())
        FakeSummaryService.release.set()
        closer.join(2)
        self.assertFalse(closer.is_alive())
        self.assertFalse((self.directory / "AI总结.json").exists())

    def test_two_windows_do_not_submit_the_same_job_simultaneously(self):
        FakeSummaryService.release = threading.Event()
        self.controller.dispatch("summarize", {"api_key": "mock-only"})
        self.assertTrue(FakeSummaryService.entered.wait(2))
        second = self.fresh_controller()
        try:
            try:
                second.dispatch("summarize", {"api_key": "mock-only"})
            except (ValueError, RuntimeError):
                pass
            else:
                state = wait_idle(second, timeout=1)
                self.assertEqual(state["selected_job"]["ai_summary"]["status"], "error")
            self.assertEqual(len(FakeSummaryService.calls), 1)
        finally:
            FakeSummaryService.release.set()
            wait_idle(second)
            wait_idle(self.controller)

    def test_summary_argument_types_and_open_report_path(self):
        for payload in [
            {"api_key": []}, {"api_key": None}, {"api_key": "mock-only", "allow_partial": "false"},
            {"api_key": "mock-only", "allow_partial": 1}, {"api_key": ""},
        ]:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                self.controller.dispatch("summarize", payload)
        self.assertEqual(FakeSummaryService.calls, [])
        with self.assertRaises(ValueError):
            self.controller.dispatch("open_summary", {})
        self.generate()
        self.controller.dispatch("open_summary", {})
        self.assertEqual(self.opened, [self.directory / "AI总结.md"])

    def test_old_history_can_be_summarized_without_reanalysis(self):
        analysis_path = self.directory / "analysis.json"
        analysis = json.loads(analysis_path.read_text(encoding="utf-8"))
        analysis["fingerprint"]["workflow_version"] = "old-qa-version"
        atomic_json(analysis_path, analysis)
        original = (self.directory / "checkpoint.json").read_bytes()
        self.assertTrue(self.controller.state()["selected_job"]["old_version"])
        result = self.generate()
        self.assertEqual(result["status"], "complete")
        self.assertEqual((self.directory / "checkpoint.json").read_bytes(), original)


class SummaryHttpTests(unittest.TestCase):
    # Share isolated fixture helpers without inheriting the controller tests.
    setUp = SummaryControllerTests.setUp
    fresh_controller = SummaryControllerTests.fresh_controller
    tearDown = SummaryControllerTests.tearDown

    def test_summary_post_requires_token_and_rejects_non_boolean_partial(self):
        dist = self.root / "web" / "dist"
        dist.mkdir(parents=True)
        (dist / "index.html").write_text("<html><head></head></html>", encoding="utf-8")
        server = DesktopServer(self.controller, dist).start()
        try:
            base = server.url.rstrip("/")
            payload = {"api_key": "mock-only", "allow_partial": "false"}
            for token, expected in [(False, 403), (True, 400)]:
                headers = {"Content-Type": "application/json", "Origin": base}
                if token:
                    headers["X-Meeting-Token"] = server.token
                req = request.Request(base + "/api/summarize", data=json.dumps(payload).encode(),
                                      headers=headers, method="POST")
                with self.assertRaises(error.HTTPError) as caught:
                    request.urlopen(req, timeout=2)
                self.assertEqual(caught.exception.code, expected)
                caught.exception.close()
            self.assertEqual(FakeSummaryService.calls, [])
        finally:
            server.shutdown()


if __name__ == "__main__":
    unittest.main()
