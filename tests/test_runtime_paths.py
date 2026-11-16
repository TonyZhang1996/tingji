"""Installed-runtime isolation and helper dispatch using temporary fixtures."""
from __future__ import annotations

from contextlib import ExitStack
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import desktop
from meeting_tool import audio, runtime_paths, speaker_backend


class RuntimePathTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="tingji-runtime-")
        self.root = Path(self.temp.name)
        self.install = self.root / "installed"
        self.resources = self.install / "_internal"
        self.profile = self.root / "clean-profile"
        self.resources.mkdir(parents=True)
        self.stack = ExitStack()
        self.stack.enter_context(patch.object(sys, "frozen", True, create=True))
        self.stack.enter_context(patch.object(sys, "_MEIPASS", str(self.resources), create=True))
        self.stack.enter_context(patch.object(sys, "executable", str(self.install / "Tingji.exe")))
        self.stack.enter_context(patch.dict(os.environ, {
            "LOCALAPPDATA": str(self.profile), "MEETING_SPEAKER_PYTHON": "untrusted-python.exe",
            "MEETING_SPEAKER_ROOT": "untrusted-models", "PYTHONHOME": "untrusted-home",
            "PYTHONPATH": "untrusted-path", "DEEPSEEK_API_KEY": "private-unit-test-only"}))

    def tearDown(self):
        self.stack.close()
        self.temp.cleanup()

    def test_frozen_resources_and_private_data_use_different_roots(self):
        self.assertEqual(runtime_paths.resource_path("web", "dist"), self.resources / "web" / "dist")
        self.assertEqual(runtime_paths.user_data_root(), self.profile / "Tingji")
        self.assertFalse(runtime_paths.user_data_root().is_relative_to(self.install))
        self.assertEqual(runtime_paths.speaker_executable(), self.install / "TingjiSpeaker.exe")

    def test_source_checkout_preserves_existing_data_location(self):
        with patch.object(sys, "frozen", False):
            self.assertEqual(runtime_paths.resource_root(), Path(desktop.__file__).resolve().parent)
            self.assertEqual(runtime_paths.user_data_root(), runtime_paths.source_root())

    def test_bundled_ffmpeg_never_uses_external_path(self):
        binary = self.resources / "bin" / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
        binary.parent.mkdir()
        binary.write_bytes(b"synthetic executable marker")
        with patch.object(audio.shutil, "which", return_value="untrusted-ffmpeg") as which:
            self.assertEqual(audio.find_binary("ffmpeg"), str(binary))
            which.assert_not_called()
            with self.assertRaisesRegex(RuntimeError, "重新安装"):
                audio.find_binary("ffprobe")
            which.assert_not_called()

    def test_bundled_worker_ignores_developer_environment(self):
        worker = self.install / "TingjiSpeaker.exe"
        worker.write_bytes(b"synthetic helper marker")
        model = self.resources / "models/3d-speaker/pretrained_models/campplus/campplus_cn_en_common.pt"
        model.parent.mkdir(parents=True)
        model.write_bytes(b"synthetic model marker")
        self.assertEqual(speaker_backend._configuration(), (worker, model.parents[2]))
        self.assertTrue(speaker_backend.environment_status()[0])
        model.unlink()
        self.assertIn("重新安装", speaker_backend.environment_status()[1])

    def test_worker_environment_resets_bootloader_and_drops_private_configuration(self):
        env = runtime_paths.speaker_child_environment()
        self.assertEqual(env["PYINSTALLER_RESET_ENVIRONMENT"], "1")
        self.assertEqual(env["HF_HUB_OFFLINE"], "1")
        for key in ("PYTHONHOME", "PYTHONPATH", "MEETING_SPEAKER_PYTHON", "MEETING_SPEAKER_ROOT", "DEEPSEEK_API_KEY"):
            self.assertNotIn(key, env)
        self.assertIn("DEEPSEEK_API_KEY", os.environ)

    def test_worker_mode_never_opens_desktop_or_reads_settings(self):
        with patch("meeting_tool.diarization_worker.main", return_value=7) as worker, patch.object(desktop, "MeetingController") as controller:
            self.assertEqual(desktop.main(["--speaker-worker", "--wav", "synthetic.wav"]), 7)
            worker.assert_called_once_with(["--wav", "synthetic.wav"])
            controller.assert_not_called()

    def test_explicit_self_check_does_not_construct_a_controller(self):
        report = self.root / "check.json"
        with patch("meeting_tool.runtime_check.check_runtime", return_value=0) as check, patch.object(desktop, "MeetingController") as controller:
            self.assertEqual(desktop.main(["--self-check", str(report)]), 0)
            check.assert_called_once_with(report)
            controller.assert_not_called()

    def test_frozen_diarize_uses_hidden_internal_helper_and_progress_pipe(self):
        source = self.root / "synthetic.wav"
        source.write_bytes(b"synthetic audio marker")
        work = self.root / "job"
        work.mkdir()
        (work / "diarization.json").write_text(json.dumps({"turns": [
            {"start": 0, "end": 1, "speaker": "S1", "uncertain": False}]}), encoding="utf-8")
        class Process:
            stdout = io.StringIO('{"event":"progress","message":"synthetic progress"}\n')
            def poll(self): return 0
            def wait(self): return 0
        logs = []
        with patch.object(speaker_backend, "environment_status", return_value=(True, "ok")), \
                patch.object(speaker_backend.subprocess, "Popen", return_value=Process()) as popen:
            turns = speaker_backend.diarize(source, work, 1, log=logs.append)
        args, kwargs = popen.call_args
        self.assertEqual(args[0][:2], [str(self.install / "TingjiSpeaker.exe"), "--speaker-worker"])
        self.assertNotIn("-X", args[0])
        self.assertEqual(kwargs["env"]["PYINSTALLER_RESET_ENVIRONMENT"], "1")
        self.assertEqual(logs, ["synthetic progress"])
        self.assertEqual(turns[0]["speaker"], "S1")


if __name__ == "__main__":
    unittest.main()
