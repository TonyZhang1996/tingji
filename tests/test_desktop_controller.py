"""Desktop orchestration regressions. No real model, network API, or user data."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
import wave
from unittest.mock import patch

from meeting_tool.desktop_controller import MeetingController
from meeting_tool.engine import DIARIZATION_VERSION, JobLock, RunConfig, job_directory, source_fingerprint
from meeting_tool.exports import atomic_json, export_transcript


def create_audio(path: Path, seconds: float = 5.0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as writer:
        writer.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        writer.writeframes(b"\x01\x00" * int(seconds * 16000))
    return path


def prepare_analysis(config: RunConfig) -> dict:
    directory = job_directory(config)
    recording = directory / "cache" / "recording_16k.wav"
    recording.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(config.audio_path, recording)
    turns = [
        {"start": 0.1, "end": 1.8, "speaker": "S1", "uncertain": False},
        {"start": 2.0, "end": 4.6, "speaker": "S2", "uncertain": True},
    ]
    samples = []
    for turn in turns:
        sample = directory / "人物试听片段" / (turn["speaker"] + ".wav")
        create_audio(sample, turn["end"] - turn["start"])
        samples.append({"speaker": turn["speaker"], "path": str(sample),
                        "start": turn["start"], "end": turn["end"], "uncertain": True})
    analysis = {"schema_version": 2, "fingerprint": source_fingerprint(config),
                "diarization_version": DIARIZATION_VERSION, "job_dir": str(directory),
                "wav_path": str(recording), "duration_seconds": 5.0,
                "detected_speech_seconds": 4.3, "num_speakers_requested": 2,
                "num_speakers_found": 2, "turns": turns, "samples": samples,
                "source_info": {"path": str(config.audio_path), "duration_seconds": 5.0,
                                "sample_rate": 16000, "channels": 1}, "warnings": []}
    atomic_json(directory / "analysis.json", analysis)
    return analysis


def prepare_complete(config: RunConfig) -> dict:
    analysis = prepare_analysis(config)
    metadata = {"audio_path": str(config.audio_path), "duration_seconds": 5.0,
                "num_speakers_requested": 2, "num_speakers_found": 2}
    segments = []
    for index, turn in enumerate(analysis["turns"]):
        identifier = hashlib.sha256(json.dumps(turn, sort_keys=True).encode()).hexdigest()[:20]
        segments.append({"id": identifier, "index": index, **turn,
                         "state": "done", "text": "自动原文" + str(index + 1)})
    atomic_json(Path(analysis["job_dir"]) / "checkpoint.json",
                {"schema_version": 1, "fingerprint": source_fingerprint(config),
                 "metadata": metadata, "segments": segments})
    return export_transcript(Path(analysis["job_dir"]), metadata, segments, complete=True)


class FakeJob:
    calls: list[str] = []
    entered = threading.Event()
    release: threading.Event | None = None
    echo_key_error = False

    def __init__(self, config, log=None, progress=None, cancel_event=None):
        self.config, self.log = config, log or (lambda _text: None)
        self.progress = progress or (lambda _data: None)
        self.cancel = cancel_event
        self.job_dir = job_directory(config)

    def analyze_only(self):
        self.calls.append("analyze")
        self.entered.set()
        if self.release:
            self.release.wait(4)
        return prepare_analysis(self.config)

    def run(self):
        self.calls.append("transcribe")
        self.entered.set()
        if self.release:
            self.release.wait(4)
        if self.echo_key_error:
            self.log("测试传输错误：" + self.config.api_key)
            raise RuntimeError("测试错误：" + self.config.api_key)
        self.progress({"stage": "transcribe", "current": 2, "total": 2, "message": "已保存模拟文字"})
        return prepare_complete(self.config)


def wait_idle(controller, timeout=5.0):
    deadline = time.monotonic() + timeout
    while controller.state()["task"]["running"]:
        if time.monotonic() >= deadline:
            raise AssertionError("Desktop worker did not become idle")
        threading.Event().wait(0.02)
    return controller.state()


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="meeting_desktop_test_")
        self.root = Path(self.temp.name)
        self.audio = create_audio(self.root / "source.wav")
        self.output = self.root / "results"
        self.output.mkdir()
        self.settings = self.root / "settings.json"
        atomic_json(self.settings, {"output_root": str(self.output), "num_speakers": 2,
                                    "max_workers": 1, "hourly_price": 0.39})
        FakeJob.calls = []
        FakeJob.entered = threading.Event()
        FakeJob.release = None
        FakeJob.echo_key_error = False
        self.controller = MeetingController(self.root, settings_path=self.settings, job_factory=FakeJob)

    def tearDown(self):
        if FakeJob.release:
            FakeJob.release.set()
        wait_idle(self.controller)
        self.temp.cleanup()

    def select_source(self):
        self.controller.select_audio(self.audio)

    def create_selected_complete(self):
        config = RunConfig(self.audio, self.output, num_speakers=2)
        prepare_complete(config)
        entries = self.controller.state()["jobs"]
        self.assertEqual(len(entries), 1)
        self.controller.dispatch("select_job", {"id": entries[0]["id"]})
        return job_directory(config)

    def test_analyze_uses_offline_method_without_key_or_paid_ledger(self):
        self.select_source()
        self.controller.dispatch("analyze", {})
        state = wait_idle(self.controller)
        self.assertEqual(FakeJob.calls, ["analyze"])
        self.assertEqual(len(state["selected_job"]["segments"]), 2)
        self.assertTrue(all(row["state"] == "pending" for row in state["selected_job"]["segments"]))
        self.assertFalse(any(self.output.rglob("checkpoint.json")))

    def test_rename_and_segment_review_preserve_original_paid_ledger(self):
        directory = self.create_selected_complete()
        checkpoint = directory / "checkpoint.json"
        original = checkpoint.read_bytes()
        state = self.controller.dispatch("speaker_names", {"names": {"S1": "甲", "S2": "乙"}})
        first = state["selected_job"]["segments"][0]
        self.controller.dispatch("segment_review", {"id": first["id"], "text": "人工校正文字", "speaker": "S2", "reviewed": True})
        result = self.controller.state()["selected_job"]
        self.assertEqual(result["segments"][0]["text"], "人工校正文字")
        self.assertEqual(result["segments"][0]["speaker"], "S2")
        self.assertTrue(result["segments"][0]["reviewed"])
        self.assertEqual(checkpoint.read_bytes(), original)
        self.assertTrue((directory / "review_annotations.json").is_file())
        exported = (directory / "完整转写.txt").read_text(encoding="utf-8-sig")
        self.assertIn("人工校正文字", exported)
        self.assertIn("乙", exported)
        # A fresh controller restores annotations; no model/API is involved.
        restored = MeetingController(self.root, settings_path=self.settings, job_factory=FakeJob)
        restored.dispatch("select_job", {"id": restored.state()["jobs"][0]["id"]})
        self.assertEqual(restored.state()["selected_job"]["segments"][0]["text"], "人工校正文字")
        # Human confirmation updates display/export flags, never the machine ledger.
        second = restored.state()["selected_job"]["segments"][1]
        self.assertTrue(second["uncertain"])
        confirmed = restored.dispatch("segment_review", {"id": second["id"], "reviewed": True})
        self.assertFalse(confirmed["selected_job"]["segments"][1]["uncertain"])
        self.assertTrue(confirmed["selected_job"]["segments"][1]["machine_uncertain"])
        exported_json = json.loads((directory / "完整转写.json").read_text(encoding="utf-8"))
        self.assertFalse(exported_json["segments"][1]["uncertain"])
        undone = restored.dispatch("segment_review", {"id": second["id"], "reviewed": False})
        self.assertTrue(undone["selected_job"]["segments"][1]["uncertain"])
        self.assertEqual(checkpoint.read_bytes(), original)
        self.assertEqual(FakeJob.calls, [])

    def test_selecting_new_source_does_not_keep_previous_selected_job(self):
        self.create_selected_complete()
        other = create_audio(self.root / "other.wav")
        self.controller.select_audio(other)
        state = self.controller.state()
        self.assertEqual(Path(state["source"]["path"]), other)
        self.assertIsNone(state["selected_job"])

    def test_key_is_redacted_from_state_logs_errors_and_files(self):
        self.select_source()
        FakeJob.echo_key_error = True
        secret = "mock-secret-qa-do-not-persist-7b45"
        self.controller.dispatch("transcribe", {"api_key": secret})
        state = wait_idle(self.controller)
        self.assertNotIn(secret, json.dumps(state, ensure_ascii=False))
        for path in self.root.rglob("*"):
            if path.is_file():
                self.assertNotIn(secret.encode(), path.read_bytes(), str(path))

    def test_cancel_keeps_task_busy_until_inflight_worker_finishes(self):
        self.select_source()
        FakeJob.release = threading.Event()
        self.controller.dispatch("transcribe", {"api_key": "mock-qa-only"})
        self.assertTrue(FakeJob.entered.wait(2))
        state = self.controller.dispatch("cancel", {})
        self.assertTrue(state["task"]["running"])
        self.assertTrue(state["task"]["cancel_requested"])
        with self.assertRaises(Exception):
            self.controller.dispatch("analyze", {})
        FakeJob.release.set()
        state = wait_idle(self.controller)
        self.assertFalse(state["task"]["running"])
        self.assertTrue(all(row["state"] == "done" for row in state["selected_job"]["segments"]))

    def test_config_rejects_secret_and_invalid_concurrency(self):
        original = self.settings.read_bytes()
        with self.assertRaises(Exception):
            self.controller.dispatch("config", {"api_key": "do-not-write"})
        self.assertEqual(self.settings.read_bytes(), original)
        for payload in ({"max_workers": 0}, {"num_speakers": -1}, {"hourly_price": -1}):
            with self.assertRaises(Exception):
                self.controller.dispatch("config", payload)
        self.assertEqual(self.settings.read_bytes(), original)

    def test_delete_last_job_removes_results_and_caches_but_preserves_source(self):
        directory = self.create_selected_complete()
        original = self.audio.read_bytes()
        job = self.controller.state()["selected_job"]
        identifier = job["id"]
        atomic_json(directory / "review_annotations.json", {"segments": {}})
        atomic_json(directory / "ai_summary.json", {"demo": "summary"})
        self.controller._ai_summaries[identifier] = {"status": "complete"}
        media = list(self.controller._media)
        state = self.controller.dispatch("delete_job", {"job_id": identifier})
        self.assertFalse(directory.exists())
        self.assertEqual(self.audio.read_bytes(), original)
        self.assertEqual(state["jobs"], [])
        self.assertIsNone(state["selected_job"])
        self.assertIsNone(state["source"])
        self.assertNotIn(identifier, self.controller._summary_cache)
        self.assertNotIn(identifier, self.controller._ai_summaries)
        self.assertFalse(set(media) & set(self.controller._media))
        self.assertEqual(self.controller.state()["jobs"], [])
        restored = MeetingController(self.root, settings_path=self.settings, job_factory=FakeJob)
        self.assertEqual(restored.state()["jobs"], [])
        # A recreated task at the same path cannot reuse the deleted JSON cache.
        prepare_complete(RunConfig(self.audio, self.output, num_speakers=2))
        entries = self.controller.state()["jobs"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["id"], identifier)

    def test_delete_selected_job_opens_remaining_job(self):
        directory = self.create_selected_complete()
        identifier = self.controller.state()["selected_job"]["id"]
        other = create_audio(self.root / "other.wav")
        other_config = RunConfig(other, self.output, num_speakers=2)
        prepare_complete(other_config)
        self.controller.state()
        state = self.controller.dispatch("delete_job", {"job_id": identifier})
        self.assertFalse(directory.exists())
        self.assertTrue(job_directory(other_config).is_dir())
        self.assertEqual(len(state["jobs"]), 1)
        self.assertEqual(state["selected_job"]["id"], state["jobs"][0]["id"])
        self.assertEqual(Path(state["source"]["path"]), other)

    def test_delete_nonselected_job_keeps_current_selection(self):
        directory = self.create_selected_complete()
        identifier = self.controller.state()["selected_job"]["id"]
        other = create_audio(self.root / "other.wav")
        other_config = RunConfig(other, self.output, num_speakers=2)
        prepare_complete(other_config)
        entries = self.controller.state()["jobs"]
        other_id = next(row["id"] for row in entries if row["id"] != identifier)
        state = self.controller.dispatch("delete_job", {"job_id": other_id})
        self.assertTrue(directory.is_dir())
        self.assertFalse(job_directory(other_config).exists())
        self.assertTrue(other.is_file())
        self.assertEqual(state["selected_job"]["id"], identifier)
        self.assertEqual(Path(state["source"]["path"]), self.audio)

    def test_delete_rejects_busy_task_and_other_process_lock(self):
        directory = self.create_selected_complete()
        identifier = self.controller.state()["selected_job"]["id"]
        checkpoint = (directory / "checkpoint.json").read_bytes()
        self.controller._task["running"] = True
        try:
            with self.assertRaisesRegex(ValueError, "仍在运行"):
                self.controller.dispatch("delete_job", {"job_id": identifier})
        finally:
            self.controller._task["running"] = False
        with JobLock(directory):
            with self.assertRaisesRegex(ValueError, "另一窗口中使用"):
                self.controller.dispatch("delete_job", {"job_id": identifier})
        self.assertEqual((directory / "checkpoint.json").read_bytes(), checkpoint)
        self.assertTrue(self.audio.is_file())

    def test_delete_rejects_unknown_ids_payloads_and_previous_output_root(self):
        directory = self.create_selected_complete()
        identifier = self.controller.state()["selected_job"]["id"]
        for payload in ({}, {"job_id": "../results"}, {"job_id": identifier, "path": str(directory)}, {"job_id": 1}):
            with self.assertRaises(ValueError):
                self.controller.dispatch("delete_job", payload)
            self.assertTrue((directory / "analysis.json").is_file())
        other_root = self.root / "other-results"
        other_root.mkdir()
        self.controller.dispatch("config", {"output_root": str(other_root)})
        with self.assertRaisesRegex(ValueError, "先打开该任务"):
            self.controller.dispatch("delete_job", {"job_id": identifier})
        self.assertTrue(directory.is_dir())

    def test_delete_protects_source_audio_from_all_supported_metadata(self):
        directory = self.create_selected_complete()
        identifier = self.controller.state()["selected_job"]["id"]
        inside = create_audio(directory / "original.wav")
        for filename, section, field in (
            ("analysis.json", "fingerprint", "path"),
            ("analysis.json", "source_info", "path"),
            ("checkpoint.json", "metadata", "audio_path"),
            ("完整转写.json", "metadata", "audio_path"),
        ):
            with self.subTest(filename=filename, section=section):
                file = directory / filename
                original = json.loads(file.read_text(encoding="utf-8"))
                updated = json.loads(json.dumps(original))
                updated.setdefault(section, {})[field] = str(inside)
                atomic_json(file, updated)
                with self.assertRaisesRegex(ValueError, "原始录音"):
                    self.controller.dispatch("delete_job", {"job_id": identifier})
                self.assertTrue(inside.is_file())
                self.assertTrue((directory / "cache" / "recording_16k.wav").is_file())
                atomic_json(file, original)

    def test_delete_protects_audio_imported_by_another_task_or_current_source(self):
        directory = self.create_selected_complete()
        identifier = self.controller.state()["selected_job"]["id"]
        inside = directory / "cache" / "recording_16k.wav"
        other_config = RunConfig(inside, self.output, num_speakers=2)
        prepare_complete(other_config)
        self.controller.state()
        with self.assertRaisesRegex(ValueError, "原始录音"):
            self.controller.dispatch("delete_job", {"job_id": identifier})
        self.assertTrue(inside.is_file())
        self.controller._jobs = {identifier: directory}
        self.controller._source = {"path": str(inside)}
        with self.assertRaisesRegex(ValueError, "原始录音"):
            self.controller.dispatch("delete_job", {"job_id": identifier})
        self.assertTrue(inside.is_file())

    def test_delete_rejects_nested_directory_links_before_removing_anything(self):
        directory = self.create_selected_complete()
        identifier = self.controller.state()["selected_job"]["id"]
        outside = self.root / "outside"
        outside.mkdir()
        sentinel = outside / "keep.txt"
        sentinel.write_text("keep", encoding="utf-8")
        link = directory / "cache" / "linked"
        if os.name == "nt":
            subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)], check=True, capture_output=True)
        else:
            link.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "目录链接"):
            self.controller.dispatch("delete_job", {"job_id": identifier})
        self.assertTrue((directory / "analysis.json").is_file())
        self.assertTrue((directory / "cache" / "recording_16k.wav").is_file())
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")

    def test_delete_io_failure_reports_failure_and_preserves_registry(self):
        directory = self.create_selected_complete()
        identifier = self.controller.state()["selected_job"]["id"]
        with patch("meeting_tool.desktop_controller.shutil.rmtree", side_effect=PermissionError):
            with self.assertRaisesRegex(ValueError, "文件被占用"):
                self.controller.dispatch("delete_job", {"job_id": identifier})
        self.assertTrue((directory / "analysis.json").is_file())
        self.assertIn(identifier, self.controller._jobs)
        self.assertTrue(self.audio.is_file())


if __name__ == "__main__":
    unittest.main()
