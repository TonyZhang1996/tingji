"""Publication boundary tests; only synthetic files, no build or user data."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts" / "windows"
sys.path.insert(0, str(SCRIPTS))
import build_windows as build
from prepare_vendor_manifest import SPEAKER_FILES, WEBVIEW, digest

publication_spec = importlib.util.spec_from_file_location("publication_guard", SCRIPTS.parent / "check_publish.py")
publication = importlib.util.module_from_spec(publication_spec)
publication_spec.loader.exec_module(publication)


class WindowsPackagingTests(unittest.TestCase):
    def test_source_publication_allows_only_reviewed_build_files(self):
        for name in ("requirements_windows.txt", "scripts/windows/README.md",
                     "scripts/windows/build_windows.py", "scripts/windows/prepare_vendor_manifest.py",
                     "scripts/windows/tingji.spec", "scripts/windows/tingji.iss"):
            self.assertTrue(publication.allowed(name), name)
        for name in ("scripts/windows/settings.json", "scripts/windows/.env", "scripts/windows/notes.md",
                     "scripts/windows/vendor/model.onnx", "scripts/windows/recording.wav",
                     "scripts/windows/Tingji-Setup.exe", "work/windows-installer/build/source.py"):
            self.assertFalse(publication.allowed(name), name)

    def test_only_runtime_source_and_branding_are_staged(self):
        for name in ("desktop.py", "meeting_tool/runtime_paths.py", "assets/tingji.ico", "web/dist/assets/index-example.js"):
            self.assertTrue(build.source_allowed(name))
        for name in ("settings.json", ".env", "app.py", "work/recording.wav", "转写结果/完整转写.md",
                     "web/src/private.ts", "assets/personal.png", "web/dist/assets/recording.mp3"):
            self.assertFalse(build.source_allowed(name))

    def test_vendor_hashes_and_manifest_path_boundary_are_enforced(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = [("3d-speaker/" + name, "speaker") for name in SPEAKER_FILES]
            paths += [(WEBVIEW, "prerequisite"), ("ffmpeg/bin/ffmpeg.exe", "ffmpeg"),
                      ("ffmpeg/bin/ffprobe.exe", "ffmpeg"), ("licenses/NOTICE.txt", "license")]
            entries = []
            for name, role in paths:
                file = root / name
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_bytes(b"fixture")
                entries.append({"path": name, "role": role, "bytes": 7, "sha256": digest(file)})
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({"schema_version": 1, "files": entries}), encoding="utf-8")
            self.assertEqual(len(build.read_vendor(root)["files"]), len(entries))
            (root / "ffmpeg/bin/ffprobe.exe").write_bytes(b"changed")
            with self.assertRaisesRegex(ValueError, "hash"):
                build.read_vendor(root)
            entries[0]["path"] = "../outside.py"
            manifest.write_text(json.dumps({"schema_version": 1, "files": entries}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "path"):
                build.read_vendor(root)

    def test_build_child_environment_does_not_inherit_account_or_model_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "fixture-key", "GH_TOKEN": "fixture-token",
                                         "MEETING_SPEAKER_ROOT": "private-model-dir", "PYTHONPATH": "private-python-dir"}):
                env = build.clean_environment(work, work / "build-venv/Scripts/python.exe")
            for name in ("DEEPSEEK_API_KEY", "GH_TOKEN", "MEETING_SPEAKER_ROOT", "PYTHONPATH"):
                self.assertNotIn(name, env)
            self.assertEqual(env["PYTHONNOUSERSITE"], "1")
            self.assertTrue(Path(env["USERPROFILE"]).is_relative_to(work))
            self.assertTrue(Path(env["LOCALAPPDATA"]).is_relative_to(work))

    def test_frozen_artifact_rejects_private_settings_media_and_credentials(self):
        for name, payload in (("settings.json", b"{}"), ("meeting.wav", b"fixture"),
                              ("AI总结.md", b"fixture"), ("fixture.dll", ("sk-" + "X" * 32).encode())):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / name).write_bytes(payload)
                with self.assertRaises(ValueError):
                    build.audit_tree(root)


if __name__ == "__main__":
    unittest.main()
