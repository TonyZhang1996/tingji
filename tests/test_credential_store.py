"""Synthetic credentials only; every vault lives in a test temporary directory."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from meeting_tool.credential_store import CredentialStore, CredentialStoreError, default_credential_path
from meeting_tool.desktop_controller import MeetingController
from meeting_tool.engine import RunConfig
from meeting_tool.exports import atomic_json
from meeting_tool.runtime_paths import source_root
from test_desktop_controller import FakeJob, create_audio, prepare_complete, wait_idle
from test_desktop_summary import FakeSummaryService


@unittest.skipUnless(os.name == "nt", "Windows current-user DPAPI is required")
class WindowsCredentialStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="tingji_synthetic_vault_")
        self.root = Path(self.temp.name)
        self.store = CredentialStore(self.root / "credentials" / "fake-key.dpapi")
        self.secret = "synthetic-current-user-key-for-unit-test-4f281"

    def tearDown(self):
        self.temp.cleanup()

    def test_dpapi_ciphertext_roundtrip_after_restart_and_clear(self):
        self.assertFalse(self.store.is_saved())
        self.store.save(self.secret)
        cipher = self.store.path.read_bytes()
        self.assertNotIn(self.secret.encode(), cipher)
        self.assertNotIn(b"Tingji/DeepSeek/API-key/v1", cipher)
        restarted = CredentialStore(self.store.path)
        self.assertTrue(restarted.is_saved())
        self.assertEqual(restarted.load(), self.secret)
        self.assertEqual(list(self.store.path.parent.glob("*.tmp")), [])
        restarted.clear()
        self.assertFalse(restarted.is_saved())
        self.assertEqual(restarted.load(), "")
        restarted.clear()  # idempotent and never makes a network request

    def test_corrupt_ciphertext_is_not_treated_as_a_usable_saved_key(self):
        self.store.save(self.secret)
        original = self.store.path.read_bytes()
        self.store.path.write_bytes(original[:-8] + b"tampered")
        self.assertFalse(self.store.is_saved())
        with self.assertRaises(CredentialStoreError) as caught:
            self.store.load()
        self.assertNotIn(self.secret, str(caught.exception))
        self.assertIn("重新保存", str(caught.exception))

    def test_malformed_short_large_and_foreign_payloads_fail_closed(self):
        for value in (b"", b"plaintext-not-a-dpapi-blob", b"x" * (65536 + 1)):
            with self.subTest(size=len(value)):
                self.store.path.parent.mkdir(parents=True, exist_ok=True)
                self.store.path.write_bytes(value)
                with self.assertRaises(CredentialStoreError):
                    self.store.load()
                self.assertFalse(self.store.is_saved())

    def test_invalid_keys_cannot_replace_a_previous_saved_key(self):
        self.store.save(self.secret)
        before = self.store.path.read_bytes()
        for invalid in (None, [], "", "  ", "bad\nkey", "bad key", "x" * 513):
            with self.subTest(invalid_type=type(invalid).__name__):
                with self.assertRaises(CredentialStoreError):
                    self.store.save(invalid)
                self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(self.store.load(), self.secret)

    def test_failed_atomic_replace_preserves_previous_ciphertext(self):
        self.store.save(self.secret)
        before = self.store.path.read_bytes()
        with patch("meeting_tool.credential_store.os.replace", side_effect=PermissionError("fixture")):
            with self.assertRaises(CredentialStoreError):
                self.store.save("different-synthetic-key-not-sent-anywhere")
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(list(self.store.path.parent.glob("*.tmp")), [])
        self.assertEqual(self.store.load(), self.secret)

    def test_source_and_installed_defaults_use_local_appdata_but_test_apps_stay_local(self):
        fake_profile = self.root / "profile"
        with patch.dict(os.environ, {"LOCALAPPDATA": str(fake_profile)}):
            expected = fake_profile / "Tingji" / "credentials" / "deepseek-api-key.dpapi"
            self.assertEqual(default_credential_path(source_root()), expected)
            self.assertEqual(default_credential_path(fake_profile / "Tingji"), expected)
            self.assertEqual(default_credential_path(self.root / "test-app"),
                             self.root / "test-app" / "credentials" / "deepseek-api-key.dpapi")


class UnsupportedCredentialStoreTests(unittest.TestCase):
    def test_unsupported_platform_never_uses_plaintext_fallback(self):
        class Unsupported:
            supported = False
            def encrypt(self, _value):
                raise AssertionError("must not encrypt on unsupported platform")
        with tempfile.TemporaryDirectory(prefix="tingji_unsupported_vault_") as folder:
            file = Path(folder) / "fake.dpapi"
            store = CredentialStore(file, protector=Unsupported())
            self.assertFalse(store.is_saved())
            with self.assertRaisesRegex(CredentialStoreError, "不支持"):
                store.save("synthetic-unsupported-test")
            self.assertFalse(file.exists())


@unittest.skipUnless(os.name == "nt", "Windows current-user DPAPI is required")
class SavedKeyControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="tingji_saved_key_controller_")
        self.root = Path(self.temp.name)
        self.source = create_audio(self.root / "synthetic.wav")
        self.output = self.root / "results"
        self.output.mkdir()
        self.settings = self.root / "settings.json"
        atomic_json(self.settings, {"output_root": str(self.output), "num_speakers": 2})
        prepare_complete(RunConfig(self.source, self.output, num_speakers=2))
        self.secret = "synthetic-controller-saved-key-never-network-59b7"
        self.keys_seen = []
        FakeJob.calls = []
        FakeJob.release = None
        FakeJob.echo_key_error = False
        FakeJob.entered = threading.Event()
        FakeSummaryService.calls = []
        FakeSummaryService.retry_flags = []
        FakeSummaryService.release = None
        FakeSummaryService.fail = False
        FakeSummaryService.omit_people = False
        self.controller = self.fresh()
        self.assertTrue(self.controller._credential_store.path.is_relative_to(self.root))

    def fresh(self):
        def fake_job(config, **kwargs):
            self.keys_seen.append(("transcribe", config.api_key))
            return FakeJob(config, **kwargs)
        def fake_summary(api_key, **kwargs):
            self.keys_seen.append(("summary", api_key))
            return FakeSummaryService(api_key, **kwargs)
        return MeetingController(self.root, settings_path=self.settings, job_factory=fake_job,
                                 summary_factory=fake_summary)

    def tearDown(self):
        wait_idle(self.controller)
        self.temp.cleanup()

    def assert_no_plaintext(self):
        self.assertNotIn(self.secret, json.dumps(self.controller.state(), ensure_ascii=False))
        for file in self.root.rglob("*"):
            if file.is_file():
                self.assertNotIn(self.secret.encode(), file.read_bytes(), file.name)

    def test_save_restart_and_clear_only_expose_boolean_status(self):
        self.assertFalse(self.controller.state()["api_key_saved"])
        state = self.controller.dispatch("save_api_key", {"api_key": self.secret})
        self.assertTrue(state["api_key_saved"])
        self.assertEqual(self.keys_seen, [])  # save does not call providers
        self.assert_no_plaintext()
        restarted = self.fresh()
        self.assertTrue(restarted.state()["api_key_saved"])
        restarted.dispatch("clear_api_key", {})
        self.assertFalse(self.controller.state()["api_key_saved"])
        self.assertFalse(restarted.state()["api_key_saved"])
        with self.assertRaisesRegex(ValueError, "保存"):
            restarted.dispatch("transcribe", {})
        self.assertEqual(self.keys_seen, [])

    def test_blank_transcribe_uses_saved_key_but_explicit_override_is_not_saved(self):
        self.controller.dispatch("save_api_key", {"api_key": self.secret})
        self.controller.dispatch("transcribe", {})
        wait_idle(self.controller)
        self.assertEqual(self.keys_seen[-1], ("transcribe", self.secret))
        explicit = "synthetic-explicit-once-only-key"
        self.controller.dispatch("transcribe", {"api_key": explicit})
        wait_idle(self.controller)
        self.assertEqual(self.keys_seen[-1], ("transcribe", explicit))
        self.assertEqual(self.controller._credential_store.load(), self.secret)
        self.controller.dispatch("transcribe", {"api_key": "   "})
        wait_idle(self.controller)
        self.assertEqual(self.keys_seen[-1], ("transcribe", self.secret))
        self.assert_no_plaintext()

    def test_blank_summary_uses_saved_key_without_exposing_it(self):
        self.controller.dispatch("save_api_key", {"api_key": self.secret})
        self.controller.dispatch("summarize", {})
        state = wait_idle(self.controller)
        self.assertEqual(self.keys_seen[-1], ("summary", self.secret))
        self.assertEqual(state["selected_job"]["ai_summary"]["status"], "complete")
        self.assert_no_plaintext()

    def test_corrupt_saved_key_stops_both_operations_before_any_provider(self):
        self.controller.dispatch("save_api_key", {"api_key": self.secret})
        self.controller._credential_store.path.write_bytes(b"synthetic-corrupt-vault-fixture")
        self.assertFalse(self.controller.state()["api_key_saved"])
        for action in ("transcribe", "summarize"):
            with self.subTest(action=action):
                with self.assertRaisesRegex(ValueError, "重新保存"):
                    self.controller.dispatch(action, {})
        self.assertEqual(self.keys_seen, [])
        explicit = "synthetic-explicit-despite-invalid-vault"
        self.controller.dispatch("transcribe", {"api_key": explicit})
        wait_idle(self.controller)
        self.assertEqual(self.keys_seen, [("transcribe", explicit)])
        self.assertEqual(self.controller._credential_store.path.read_bytes(), b"synthetic-corrupt-vault-fixture")

    def test_save_payload_validation_and_injected_store_are_isolated(self):
        for payload in ({}, {"api_key": self.secret, "extra": True}, {"api_key": None}):
            with self.assertRaises(ValueError):
                self.controller.dispatch("save_api_key", payload)
        self.assertFalse(self.controller.state()["api_key_saved"])
        isolated = CredentialStore(self.root / "separate" / "injected.dpapi")
        injected = MeetingController(self.root, settings_path=self.settings, credential_store=isolated)
        injected.dispatch("save_api_key", {"api_key": self.secret})
        self.assertTrue(injected.state()["api_key_saved"])
        self.assertFalse(self.controller.state()["api_key_saved"])
        self.assert_no_plaintext()


if __name__ == "__main__":
    unittest.main()
