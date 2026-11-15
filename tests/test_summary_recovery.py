"""Summary recovery regressions using bounded fake Responses HTTP payloads.

Only temporary fictional task folders are used. No real API, recording or
credential is accessed by this module.
"""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from meeting_tool.exports import atomic_json
from meeting_tool.summarization import SummaryError, SummaryService
from test_summarization import snapshot
from test_summary_client import envelope, response, wire_content


class SummaryRecoveryTests(unittest.TestCase):
    KEY = "mock-recovery-key-never-persist"
    INPUT = "source-only-request-sentinel，明天核对虚构数据。"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="meeting_recovery_qa_")
        self.job = Path(self.temp.name) / "task"
        self.job.mkdir()
        self.source = snapshot(self.INPUT)
        self.speakers = {"S1": "说话人 1"}
        self.service = SummaryService(self.KEY)
        self.previous = {
            "AI总结.json": b'{"previous":"saved report"}\n',
            "AI总结.md": "# 原有总结\n\n保留已有内容。\n".encode("utf-8"),
        }
        for name, content in self.previous.items():
            (self.job / name).write_bytes(content)

    def tearDown(self):
        self.temp.cleanup()

    def valid_wire(self):
        return wire_content(self.speakers)

    def retain_missing_field(self, job=None):
        job = job or self.job
        invalid = self.valid_wire()
        del invalid["topics"]
        with patch("meeting_tool.summary_client.request.urlopen", return_value=response(envelope(invalid))) as send:
            with self.assertRaises(SummaryError):
                self.service.generate(self.source, job)
        self.assertEqual(send.call_count, 1)
        candidates = list((job / ".ai_summary_cache" / "candidates").glob("*.json"))
        self.assertEqual(len(candidates), 1)
        return candidates[0]

    def assert_previous_unchanged(self):
        for name, content in self.previous.items():
            self.assertEqual((self.job / name).read_bytes(), content)

    def assert_no_secret_or_request_saved(self, job=None):
        job = job or self.job
        for path in job.rglob("*.json"):
            text = path.read_text(encoding="utf-8")
            self.assertNotIn(self.KEY, text)
            self.assertNotIn(self.INPUT, text)
            value = json.loads(text)
            self.assertFalse(set(value) & {"messages", "input", "headers", "api_key", "Authorization"})

    def test_completed_missing_field_is_quarantined_without_replacing_existing_report(self):
        candidate = self.retain_missing_field()
        record = json.loads(candidate.read_text(encoding="utf-8"))
        self.assertEqual(record["response_status"], "completed")
        self.assertEqual(record["candidate_format"], "wire")
        self.assertEqual(record["category"], "field_mismatch")
        self.assertEqual(record["diagnostics"]["missing_fields"], ["topics"])
        self.assertEqual(record["usage"]["total_tokens"], 20)
        self.assertNotIn("topics", record["candidate"])
        self.assertEqual(list((self.job / ".ai_summary_cache").glob("*.json")), [])
        self.assert_previous_unchanged()
        self.assert_no_secret_or_request_saved()

    def test_failed_candidate_blocks_default_network_retry_but_explicit_retry_sends_once(self):
        candidate = self.retain_missing_field()
        previous_candidate = candidate.read_bytes()
        with patch("meeting_tool.summary_client.request.urlopen") as send:
            with self.assertRaisesRegex(SummaryError, "未再次请求"):
                self.service.generate(self.source, self.job)
        send.assert_not_called()
        self.assertEqual(candidate.read_bytes(), previous_candidate)
        self.assert_previous_unchanged()
        with patch("meeting_tool.summary_client.request.urlopen",
                   return_value=response(envelope(self.valid_wire()))) as send:
            report = self.service.generate(self.source, self.job, retry_failed=True)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(report["usage"]["request_count"], 1)
        self.assertEqual([p["speaker"] for p in report["content"]["speaker_summaries"]], ["S1"])
        self.assert_previous_unchanged()
        self.assert_no_secret_or_request_saved()

    def test_corrected_completed_candidate_is_promoted_offline_without_another_request(self):
        candidate = self.retain_missing_field()
        record = json.loads(candidate.read_text(encoding="utf-8"))
        record["candidate"] = self.valid_wire()
        atomic_json(candidate, record)
        with patch("meeting_tool.summary_client.request.urlopen") as send:
            report = self.service.generate(self.source, self.job)
        send.assert_not_called()
        self.assertEqual(report["usage"]["request_count"], 0)
        self.assertEqual(report["usage"]["cache_hits"], 1)
        self.assertEqual(report["content"]["speaker_summaries"][0]["speaker_name"], "说话人 1")
        success = self.job / ".ai_summary_cache" / candidate.name
        self.assertTrue(success.is_file())
        self.assertEqual(json.loads(success.read_text(encoding="utf-8"))["content"], report["content"])
        self.assert_previous_unchanged()
        self.assert_no_secret_or_request_saved()

    def test_untrusted_or_unfinished_candidates_never_promote_or_trigger_default_network(self):
        for mode in ("corrupt", "oversized", "input_hash", "protocol", "model", "schema_hash",
                     "person_identity", "incomplete", "failed", "candidate_format"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory(prefix="meeting_candidate_qa_") as folder:
                job = Path(folder) / "task"
                job.mkdir()
                candidate = self.retain_missing_field(job)
                record = json.loads(candidate.read_text(encoding="utf-8"))
                record["candidate"] = self.valid_wire()
                if mode == "corrupt":
                    candidate.write_text("{broken JSON", encoding="utf-8")
                elif mode == "oversized":
                    candidate.write_bytes(b"x" * (1024 * 1024 + 1))
                else:
                    if mode in {"input_hash", "protocol", "model", "schema_hash"}:
                        record[mode] = "unrelated-fixture-identity"
                    elif mode == "person_identity":
                        record["candidate"]["speaker_summaries"]["S99"] = record["candidate"]["speaker_summaries"].pop("S1")
                    elif mode in {"incomplete", "failed"}:
                        record["response_status"] = mode
                    else:
                        record["candidate_format"] = "unsupported"
                    atomic_json(candidate, record)
                with patch("meeting_tool.summary_client.request.urlopen") as send:
                    with self.assertRaisesRegex(SummaryError, "未再次请求"):
                        self.service.generate(self.source, job)
                send.assert_not_called()
                self.assertFalse((job / ".ai_summary_cache" / candidate.name).exists())

    def test_candidate_write_failure_reports_possible_charge_and_never_retries(self):
        invalid = self.valid_wire()
        del invalid["topics"]
        with patch("meeting_tool.summary_client.request.urlopen", return_value=response(envelope(invalid))) as send:
            with patch("meeting_tool.summarization.atomic_json", side_effect=PermissionError("fixture disk denied")):
                with self.assertRaisesRegex(SummaryError, "本地诊断保存失败，可能已计费"):
                    self.service.generate(self.source, self.job)
        self.assertEqual(send.call_count, 1)
        self.assert_previous_unchanged()

    def test_current_key_is_redacted_from_successful_cache_and_returned_report(self):
        value = self.valid_wire()
        value["overview"] = "模型回显：" + self.KEY
        with patch("meeting_tool.summary_client.request.urlopen", return_value=response(envelope(value))) as send:
            report = self.service.generate(self.source, self.job)
        self.assertEqual(send.call_count, 1)
        self.assertNotIn(self.KEY, json.dumps(report, ensure_ascii=False))
        self.assertIn("[已隐藏]", report["content"]["overview"])
        self.assert_no_secret_or_request_saved()

    def test_canonical_validation_failure_preserves_only_redacted_candidate_and_recovers_offline(self):
        value = self.valid_wire()
        value["overview"] = "模型回显：" + self.KEY
        with patch("meeting_tool.summary_client.request.urlopen", return_value=response(envelope(value))) as send:
            with patch("meeting_tool.summarization.validate_content", side_effect=SummaryError("模拟后续语义校验失败")):
                with self.assertRaises(SummaryError):
                    self.service.generate(self.source, self.job)
        self.assertEqual(send.call_count, 1)
        candidates = list((self.job / ".ai_summary_cache" / "candidates").glob("*.json"))
        self.assertEqual(len(candidates), 1)
        record = json.loads(candidates[0].read_text(encoding="utf-8"))
        self.assertEqual(record["candidate_format"], "canonical")
        self.assertEqual(record["category"], "canonical_validation")
        self.assertIn("[已隐藏]", record["candidate"]["overview"])
        self.assert_no_secret_or_request_saved()
        self.assert_previous_unchanged()
        with patch("meeting_tool.summary_client.request.urlopen") as send:
            report = self.service.generate(self.source, self.job)
        send.assert_not_called()
        self.assertEqual(report["usage"]["request_count"], 0)
        self.assertEqual(report["usage"]["cache_hits"], 1)


if __name__ == "__main__":
    unittest.main()
