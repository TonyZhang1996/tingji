"""Exercise Android packaging privacy boundaries without a phone or user data."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile

SCRIPTS = Path(__file__).resolve().parents[1] / "mobile/scripts"
sys.path.insert(0, str(SCRIPTS))
import build_android as build
import audit_apk as audit


class AndroidPackagingTests(unittest.TestCase):
    def test_staging_only_accepts_reviewable_source_types(self):
        for name in ("app/src/main/java/com/tingji/app/MainActivity.kt", "gradle/wrapper/gradle-wrapper.jar",
                     "app/src/debug/res/xml/debug_network_security.xml"):
            self.assertTrue(build.source_allowed(name), name)
        for name in ("app/src/main/assets/recording.wav", "app/src/main/assets/settings.json", "local.properties",
                     "app/build/outputs/apk/app.apk", "app/src/main/java/com/tingji/app/key.txt"):
            self.assertFalse(build.source_allowed(name), name)

    def test_build_child_does_not_inherit_credentials_or_user_profile(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {
            "DEEPSEEK_API_KEY": "synthetic-test-value", "VITE_API_KEY": "synthetic-browser-key",
            "GRADLE_OPTS": "-Dunreviewed=hook", "USERPROFILE": "private-profile", "PATH": "private-bin",
        }, clear=True):
            root = Path(folder)
            env = build.child_environment(root, root / "jdk", root / "sdk", root / "node/node.exe")
            self.assertFalse({"DEEPSEEK_API_KEY", "VITE_API_KEY", "GRADLE_OPTS"}.intersection(env))
            self.assertNotEqual(env["USERPROFILE"], "private-profile")
            self.assertEqual(Path(env["ANDROID_USER_HOME"]), root / "android-home")
            self.assertEqual(Path(env["ANDROID_AVD_HOME"]), root / "android-avd")
            self.assertNotIn("private-bin", env["PATH"])

    @staticmethod
    def elf(alignment=16384):
        data = bytearray(120)
        data[:6] = b"\x7fELF\x02\x01"
        struct.pack_into("<Q", data, 32, 64)
        struct.pack_into("<HH", data, 54, 56, 1)
        struct.pack_into("<IIQQQQQQ", data, 64, 1, 5, 0, 0, 0, 120, 120, alignment)
        return bytes(data)

    def test_native_alignment_requires_complete_16k_load_headers(self):
        self.assertEqual(audit.elf_alignment(self.elf()), [16384])
        for invalid in (b"\x7fELF", self.elf(4096), self.elf()[:100]):
            with self.assertRaises(ValueError): audit.elf_alignment(invalid)

    def test_apk_audit_detects_actual_credentials_in_all_requested_encodings(self):
        value = "synthetic_credential_fixture_" + "0123456789"
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"TEST_API_KEY": value}, clear=True):
            root = Path(folder)
            for encoding in ("utf-8", "utf-16le", "utf-16be"):
                apk = root / "fixture.apk"
                with ZipFile(apk, "w") as archive:
                    archive.writestr("assets/web/index.html", value.encode(encoding))
                with self.assertRaisesRegex(ValueError, "Credential data") as caught:
                    audit.audit(apk, root, False)
                self.assertNotIn(value, str(caught.exception))

    def test_unknown_assets_and_personal_outputs_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {}, clear=True):
            root = Path(folder)
            for name in ("assets/private-notes.txt", "assets/summary.json", "assets/recording.wav", "assets/dexopt/meeting.prof"):
                apk = root / "fixture.apk"
                with ZipFile(apk, "w") as archive: archive.writestr(name, "public synthetic fixture")
                with self.assertRaisesRegex(ValueError, "Private data|Unreviewed application asset"):
                    audit.audit(apk, root, False)
            with ZipFile(apk, "w") as archive: archive.writestr("assets/dexopt/baseline.prof", "unrecognized content")
            with self.assertRaisesRegex(ValueError, "Invalid generated Android optimization profile"):
                audit.audit(apk, root, False)

    def test_manifest_permissions_do_not_mistake_receiver_guard_for_requested_access(self):
        text = """E: manifest
 E: uses-permission
  A: android:name(0x01010003)=\"android.permission.INTERNET\"
 E: application
  A: android:allowBackup(0x01010280)=false
  E: receiver
   A: android:permission(0x01010006)=\"android.permission.DUMP\"
"""
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            tool = root / "build-tools/35.0.0" / ("aapt2.exe" if os.name == "nt" else "aapt2")
            tool.parent.mkdir(parents=True); tool.touch()
            class Response: stdout = text
            with patch.object(audit.subprocess, "run", return_value=Response()):
                result = audit.manifest_info(root / "test.apk", root)
            self.assertEqual(result["permissions"], ["android.permission.INTERNET"])
            self.assertFalse(result["backup_enabled"])


if __name__ == "__main__": unittest.main()
