from __future__ import annotations

import io
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock
from urllib import error

from meeting_tool.audio import CancelledError
from meeting_tool.summarization import (CHUNK_CHARS, DEFAULT_MODEL, DeepSeekChatClient,
                                        SummaryError, SummaryService, _messages, _transcript_parts, _transcript_chunks,
                                        render_markdown, validate_content, PROMPT_VERSION)


def content(overview="讨论了项目计划。", *, todo=True):
    return {"overview": overview, "topics": [{"title": "项目计划", "summary": "讨论了检查数据的方式。"}],
            "speaker_summaries": [{"speaker": "unassigned", "speaker_name": "归属待核",
                                   "summary": "提出需要核对数据，发言归属待确认。", "points": ["建议先核对数据。"],
                                   "commitments": [], "evidence": "00:01:00—00:01:30"}],
            "decisions": ["先核对数据。"], "open_questions": ["负责人尚待明确。"],
            "todos": [{"task": "核对数据", "owner": "", "deadline": "明天",
                       "evidence": "00:01:00 提出需要核对数据"}] if todo else []}


def snapshot(text="明天核对数据。", *, full=True):
    return {"title": "测试会议", "duration_seconds": 180, "fingerprint": "snapshot-a",
            "coverage": {"done_count": 1, "total_count": 1 if full else 2, "full": full},
            "segments": [{"id": "S1-1", "start": 60, "end": 90, "speaker": "S1",
                          "speaker_name": "说话人 1", "text": text, "state": "done", "uncertain": True}]}


class FakeClient:
    def __init__(self, large=False, cancel=None, fail_at=None):
        self.messages = []
        self.large = large
        self.cancel = cancel
        self.fail_at = fail_at

    def complete(self, messages):
        self.messages.append(messages)
        if self.fail_at == len(self.messages):
            raise RuntimeError("mock-summary-secret-should-never-leak")
        markers = [word for word in ("开头标记", "中间标记", "末尾标记") if word in messages[-1]["content"]]
        result = content("整理：" + "、".join(markers))
        people = json.loads(messages[-1]["content"].split("\n", 1)[0])["本次人物清单"]
        result["speaker_summaries"] = [
            {"speaker": identifier, "speaker_name": name, "summary": "讨论数据检查与项目安排。",
             "points": ["建议核对数据。"], "commitments": [], "evidence": "00:01:00—00:01:30"}
            for identifier, name in people.items()]
        if self.large:
            result["decisions"] = ["内容" * 700 for _ in range(10)]
        if self.cancel is not None:
            self.cancel.set()
        return {"content": result, "usage": {"prompt_tokens": 80, "completion_tokens": 20, "total_tokens": 100}}


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def response_payload(value=None, *, finish="stop"):
    return json.dumps({"id": "completion-fixture", "choices": [{"finish_reason": finish,
                      "message": {"content": json.dumps(value or content(), ensure_ascii=False)}}],
                       "usage": {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20}},
                      ensure_ascii=False).encode("utf-8")


class SummaryServiceTests(unittest.TestCase):
    def test_short_summary_once_and_same_input_reuses_cache(self):
        with tempfile.TemporaryDirectory() as folder:
            client = FakeClient()
            progress = []
            service = SummaryService("mock", client=client)
            first = service.generate(snapshot(), Path(folder), progress=lambda *row: progress.append(row))
            second = service.generate(snapshot(), Path(folder))
            self.assertEqual(len(client.messages), 1)
            self.assertEqual(first["content"], second["content"])
            self.assertEqual(second["usage"]["request_count"], 0)
            self.assertEqual(second["usage"]["cache_hits"], 1)
            self.assertEqual(first["input_fingerprint"], "snapshot-a")
            self.assertEqual(first["model"], DEFAULT_MODEL)
            self.assertEqual(first["content"]["todos"][0]["owner"], "未明确")
            self.assertTrue(progress)
            self.assertFalse((Path(folder) / "AI总结.json").exists())

    def test_changed_text_uses_new_cache_even_with_same_external_fingerprint(self):
        with tempfile.TemporaryDirectory() as folder:
            client = FakeClient()
            service = SummaryService("mock", client=client)
            service.generate(snapshot(), Path(folder))
            service.generate(snapshot("已经修改的会议内容。"), Path(folder))
            self.assertEqual(len(client.messages), 2)

    def test_long_single_utterance_includes_tail_and_hierarchical_merges(self):
        text = "开头标记" + "甲" * 43_000 + "中间标记" + "乙" * 43_000 + "末尾标记"
        source = snapshot(text)
        chunks = _transcript_chunks(source)
        self.assertGreater(len(chunks), 6)
        self.assertTrue(all(len(chunk) <= CHUNK_CHARS for chunk in chunks))
        self.assertIn("末尾标记", chunks[-1])
        with tempfile.TemporaryDirectory() as folder:
            client = FakeClient(large=True)
            progress = []
            result = SummaryService("mock", client=client).generate(source, Path(folder),
                progress=lambda *row: progress.append(row))
            for marker in ("开头标记", "中间标记", "末尾标记"):
                self.assertIn(marker, result["content"]["overview"])
            self.assertTrue(any("第 2 轮" in row[3] for row in progress))
            self.assertEqual(result["usage"]["total_tokens"], len(client.messages) * 100)

    def test_only_done_nonempty_text_is_sent_and_partial_is_marked(self):
        source = snapshot(full=False)
        source["segments"].extend([
            {"start": 90, "end": 95, "speaker": "S2", "state": "pending", "text": "未完成内容不可发送"},
            {"start": 95, "end": 100, "speaker": "S2", "state": "done", "text": ""}])
        with tempfile.TemporaryDirectory() as folder:
            client = FakeClient()
            result = SummaryService("mock", client=client).generate(source, Path(folder))
            material = client.messages[0][-1]["content"]
            self.assertNotIn("未完成内容不可发送", material)
            self.assertIn("沿用当前自动人物标签", material)
            self.assertFalse(result["coverage"]["full"])
            self.assertIn("非整条录音总结", render_markdown(result))

    def test_cancel_after_response_caches_success_without_committing_report(self):
        with tempfile.TemporaryDirectory() as folder:
            cancelled = threading.Event()
            client = FakeClient(cancel=cancelled)
            with self.assertRaises(CancelledError):
                SummaryService("mock", client=client).generate(snapshot(), Path(folder), cancelled)
            self.assertEqual(len(list((Path(folder) / ".ai_summary_cache").glob("*.json"))), 1)
            retry_client = FakeClient()
            result = SummaryService("mock", client=retry_client).generate(snapshot(), Path(folder))
            self.assertEqual(len(retry_client.messages), 0)
            self.assertEqual(result["usage"]["cache_hits"], 1)
            self.assertFalse((Path(folder) / "AI总结.json").exists())

    def test_cancel_before_first_request_does_not_create_cache(self):
        with tempfile.TemporaryDirectory() as folder:
            cancelled = threading.Event()
            cancelled.set()
            client = FakeClient()
            with self.assertRaises(CancelledError):
                SummaryService("mock", client=client).generate(snapshot(), Path(folder), cancelled)
            self.assertFalse(client.messages)
            self.assertFalse((Path(folder) / ".ai_summary_cache").exists())

    def test_failed_attempt_resumes_successful_parts_without_key_persistence(self):
        with tempfile.TemporaryDirectory() as folder:
            source = snapshot("甲" * 30_000 + "末尾标记")
            client = FakeClient(fail_at=2)
            with self.assertRaises(SummaryError) as caught:
                SummaryService("mock-summary-secret-should-never-leak", client=client).generate(source, Path(folder))
            self.assertNotIn("mock-summary-secret", str(caught.exception))
            self.assertEqual(len(list((Path(folder) / ".ai_summary_cache").glob("*.json"))), 1)
            retry_client = FakeClient()
            result = SummaryService("mock", client=retry_client).generate(source, Path(folder))
            self.assertEqual(result["usage"]["cache_hits"], 1)
            self.assertIn("末尾标记", result["content"]["overview"])
            for path in Path(folder).rglob("*"):
                if path.is_file():
                    self.assertNotIn("mock-summary-secret", path.read_text(encoding="utf-8"))

    def test_corrupted_cache_is_ignored(self):
        with tempfile.TemporaryDirectory() as folder:
            first_client = FakeClient()
            SummaryService("mock", client=first_client).generate(snapshot(), Path(folder))
            for path in (Path(folder) / ".ai_summary_cache").glob("*.json"):
                path.write_text('{"broken":true}', encoding="utf-8")
            second_client = FakeClient()
            SummaryService("mock", client=second_client).generate(snapshot(), Path(folder))
            self.assertEqual(len(second_client.messages), 1)

    def test_markdown_tasks_always_last_and_model_markup_is_inert(self):
        report = {"title": "测试<script>", "coverage": {"full": True, "done_count": 1, "total_count": 1},
                  "content": content('<img src="https://bad.test/a"> [文字](https://bad.test)')}
        report["content"]["todos"][0]["task"] = "检查 | 一栏\n## 伪造章节"
        text = render_markdown(report)
        self.assertNotIn("<img", text)
        self.assertNotIn("<script>", text)
        self.assertIn(r"\[文字\]\(https://bad.test\)", text)
        self.assertIn(r"检查 \| 一栏 \#\# 伪造章节", text)
        self.assertEqual(text.rsplit("\n## ", 1)[-1].splitlines()[0], "待办事项")
        report["content"]["todos"] = []
        self.assertTrue(render_markdown(report).endswith("录音中没有明确的待办事项。\n"))

    def test_invalid_content_never_caches(self):
        invalid = content()
        invalid["todos"] = [{"task": "缺失其他字段"}]
        with self.assertRaises(SummaryError):
            validate_content(invalid)

    def test_stable_ids_keep_same_names_separate_and_retain_current_automatic_labels(self):
        source = snapshot("第一人的意见。")
        source["segments"][0].update(uncertain=False, speaker_name="同名")
        source["segments"] += [
            {"speaker": "S2", "speaker_name": "同名", "state": "done", "text": "第二人的意见。", "start": 95, "end": 100},
            {"speaker": "S2", "speaker_name": "同名", "state": "done", "text": "待核的发言。", "start": 101, "end": 105, "uncertain": True},
            {"speaker": "S3", "speaker_name": "已校对者", "state": "done", "text": "人工确认的意见。", "start": 106, "end": 110, "uncertain": True, "reviewed": True}]
        with tempfile.TemporaryDirectory() as folder:
            client = FakeClient()
            result = SummaryService("mock", client=client).generate(source, Path(folder))
        people = result["content"]["speaker_summaries"]
        self.assertEqual({p["speaker"] for p in people}, {"S1", "S2", "S3"})
        self.assertEqual([p["speaker_name"] for p in people if p["speaker"] in ("S1", "S2")], ["同名", "同名"])
        material = client.messages[0][-1]["content"]
        self.assertIn("[人物ID=S1] 同名", material)
        self.assertIn("[人物ID=S2] 同名", material)
        self.assertIn("[人物ID=S2] 同名（沿用当前自动人物标签）\n待核的发言。", material)
        self.assertIn("[人物ID=S3] 已校对者", material)

    def test_only_speech_without_a_valid_existing_id_uses_unassigned(self):
        source = snapshot("没有有效人物编号的发言。")
        source["segments"][0]["speaker"] = "unknown"
        with tempfile.TemporaryDirectory() as folder:
            result = SummaryService("mock", client=FakeClient()).generate(source, Path(folder))
        people = result["content"]["speaker_summaries"]
        self.assertEqual([person["speaker"] for person in people], ["unassigned"])
        self.assertEqual(people[0]["commitments"], [])

    def test_long_meeting_preserves_a_short_last_speaker_through_hierarchical_merge(self):
        source = snapshot("开头标记" + "甲" * 86_000 + "中间标记")
        source["segments"][0].update(uncertain=False)
        source["segments"].append({"speaker": "S2", "speaker_name": "末尾发言者", "state": "done",
                                   "text": "末尾标记，只有一次简短发言。", "start": 150, "end": 160})
        with tempfile.TemporaryDirectory() as folder:
            client = FakeClient(large=True)
            result = SummaryService("mock", client=client).generate(source, Path(folder))
        self.assertEqual({p["speaker"] for p in result["content"]["speaker_summaries"]}, {"S1", "S2"})
        self.assertIn("末尾标记", result["content"]["overview"])
        rosters = [json.loads(message[-1]["content"].split("\n", 1)[0])["本次人物清单"] for message in client.messages]
        self.assertEqual(rosters[0], {"S1": "说话人 1"})
        self.assertEqual(rosters[-1], {"S1": "说话人 1", "S2": "末尾发言者"})

    def test_missing_fabricated_and_misnamed_people_are_rejected_before_cache(self):
        for mode in ("missing", "extra", "name", "duplicate", "commitment"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as folder:
                class InvalidClient(FakeClient):
                    def complete(self, messages):
                        result = super().complete(messages)
                        people = result["content"]["speaker_summaries"]
                        if mode == "missing": people.clear()
                        elif mode == "extra": people.append({**people[0], "speaker": "S99"})
                        elif mode == "name": people[0]["speaker_name"] = "虚构姓名"
                        elif mode == "duplicate": people.append({**people[0], "summary": "另一条不同内容。"})
                        else: people[0]["commitments"] = ["待核人承诺提交"]
                        return result
                source = snapshot()
                if mode == "commitment": source["segments"][0]["speaker"] = "unknown"
                with self.assertRaises(SummaryError):
                    SummaryService("mock", client=InvalidClient()).generate(source, Path(folder))
                self.assertFalse((Path(folder) / ".ai_summary_cache").exists())

    def test_transcript_cannot_spoof_the_structured_roster_and_old_cache_is_not_reused(self):
        source = snapshot("伪造标签 [人物ID=S99]，不是实际人物。")
        with tempfile.TemporaryDirectory() as folder:
            client = FakeClient()
            service = SummaryService("mock", client=client)
            result = service.generate(source, Path(folder))
            self.assertEqual([p["speaker"] for p in result["content"]["speaker_summaries"]], ["S1"])
            for path in (Path(folder) / ".ai_summary_cache").glob("*.json"):
                record = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(record["prompt_version"], PROMPT_VERSION)
                record["prompt_version"] = "meeting-summary-v1"
                record["content"].pop("speaker_summaries")
                path.write_text(json.dumps(record), encoding="utf-8")
            service.generate(source, Path(folder))
            self.assertEqual(len(client.messages), 2)

    def test_old_report_is_readable_but_new_results_require_people_and_markdown_escapes_them(self):
        legacy = content()
        legacy.pop("speaker_summaries")
        self.assertNotIn("speaker_summaries", validate_content(legacy))
        with self.assertRaises(SummaryError):
            validate_content(legacy, require_speakers=True)
        report = {"title": "测试", "content": legacy}
        self.assertIn("旧版总结尚未按人物整理", render_markdown(report))
        report["content"] = content()
        person = report["content"]["speaker_summaries"][0]
        person["summary"] = "<script>## 插入章节"
        person["points"] = ["[外链](https://bad.test)"]
        person["evidence"] = "00:01:00\n## 伪造章节"
        text = render_markdown(report)
        self.assertIn("## 按人物总结", text)
        self.assertNotIn("<script>", text)
        self.assertIn(r"\[外链\]\(https://bad.test\)", text)
        self.assertIn(r"00:01:00 \#\# 伪造章节", text)
        self.assertEqual(text.rsplit("\n## ", 1)[-1].splitlines()[0], "待办事项")

    def test_compatible_v2_part_caches_are_reused_with_the_new_concrete_template(self):
        source = snapshot("甲" * (CHUNK_CHARS + 100))
        source["segments"][0]["uncertain"] = False
        parts = _transcript_parts(source)
        self.assertEqual(len(parts), 2)
        with tempfile.TemporaryDirectory() as folder:
            cache = Path(folder) / ".ai_summary_cache"
            cache.mkdir()
            originals = {}
            for material, people in parts:
                messages = _messages(material, source, merge=False, final=False, speakers=people, legacy=True)
                identity = {"prompt_version": PROMPT_VERSION, "model": DEFAULT_MODEL, "messages": messages}
                digest = hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True,
                                                  separators=(",", ":")).encode()).hexdigest()
                record = {"schema_version": 1, "prompt_version": PROMPT_VERSION, "model": DEFAULT_MODEL,
                          "input_hash": digest, "content": FakeClient().complete(messages)["content"]}
                file = cache / (digest + ".json")
                file.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
                originals[file] = file.read_bytes()
            client = FakeClient()
            report = SummaryService("mock", client=client).generate(source, Path(folder))
            self.assertEqual(report["usage"]["cache_hits"], 2)
            self.assertEqual(report["usage"]["request_count"], 1)
            self.assertEqual(len(client.messages), 1)
            system = client.messages[0][0]["content"]
            self.assertIn('"speaker":"S1"', system)
            self.assertNotIn("输入中的稳定人物编号，如S1", system)
            self.assertIn("每个编号输出唯一一项", system)
            self.assertEqual([person["speaker"] for person in report["content"]["speaker_summaries"]], ["S1"])
            for file, original in originals.items():
                self.assertEqual(file.read_bytes(), original)

    def test_number_notation_is_only_mapped_when_the_roster_mapping_is_unique(self):
        report = content()
        report["speaker_summaries"][0].update(speaker=" s 001 ", speaker_name="同名")
        normalized = validate_content(report, require_speakers=True, speakers={"S1": "同名"})
        self.assertEqual(normalized["speaker_summaries"][0]["speaker"], "S1")
        with self.assertRaises(SummaryError):
            validate_content(report, require_speakers=True, speakers={"S1": "同名", "S01": "同名"})


class SummaryHttpTests(unittest.TestCase):
    def test_real_client_normalizes_explicit_ids_and_exact_duplicate_entries_without_retry(self):
        value = content()
        person = value["speaker_summaries"][0]
        person.update(speaker=" ｓ　０１ ", speaker_name="说话人 1")
        value["speaker_summaries"].append({**person, "speaker": "S1"})
        with tempfile.TemporaryDirectory() as folder:
            with mock.patch("meeting_tool.summarization.request.urlopen", return_value=FakeResponse(response_payload(value))) as send:
                report = SummaryService("mock-key").generate(snapshot(), Path(folder))
            self.assertEqual(send.call_count, 1)
            self.assertEqual(report["usage"]["request_count"], 1)
            self.assertEqual([p["speaker"] for p in report["content"]["speaker_summaries"]], ["S1"])

    def test_real_client_combines_repeated_unassigned_passages_without_losing_content(self):
        value = content()
        first = value["speaker_summaries"][0]
        first.update(summary="第一段未归属内容。", points=["第一条观点"], evidence="00:01:00")
        value["speaker_summaries"].append({**first, "speaker": "UNASSIGNED",
                                          "summary": "第二段未归属内容。", "points": ["第二条观点"], "evidence": "00:02:00"})
        source = snapshot()
        source["segments"][0]["speaker"] = "unknown"
        with tempfile.TemporaryDirectory() as folder:
            with mock.patch("meeting_tool.summarization.request.urlopen", return_value=FakeResponse(response_payload(value))) as send:
                report = SummaryService("mock-key").generate(source, Path(folder))
        self.assertEqual(send.call_count, 1)
        people = report["content"]["speaker_summaries"]
        self.assertEqual(len(people), 1)
        for text in ("第一段未归属内容。", "第二段未归属内容。"):
            self.assertIn(text, people[0]["summary"])
        self.assertEqual(people[0]["points"], ["第一条观点", "第二条观点"])
        self.assertIn("00:01:00", people[0]["evidence"])
        self.assertIn("00:02:00", people[0]["evidence"])
        self.assertEqual(people[0]["commitments"], [])

    def test_real_client_rejects_different_concrete_duplicates_and_invalid_private_ids(self):
        for mode in ("duplicate", "invalid"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as folder:
                value = content()
                person = value["speaker_summaries"][0]
                person.update(speaker="S1", speaker_name="说话人 1")
                if mode == "duplicate":
                    value["speaker_summaries"].append({**person, "summary": "不同的另一条内容。"})
                else:
                    person["speaker"] = "private-person-sentinel"
                with mock.patch("meeting_tool.summarization.request.urlopen", return_value=FakeResponse(response_payload(value))) as send:
                    with self.assertRaises(SummaryError) as caught:
                        SummaryService("mock-key").generate(snapshot(), Path(folder))
                self.assertEqual(send.call_count, 1)
                self.assertIn("整条录音总结", str(caught.exception))
                self.assertIn("重复" if mode == "duplicate" else "格式无效", str(caught.exception))
                self.assertNotIn("private-person-sentinel", str(caught.exception))
                self.assertFalse((Path(folder) / ".ai_summary_cache").exists())

    def test_merge_failure_identifies_its_stage_and_retains_successful_part_caches(self):
        source = snapshot("甲" * (CHUNK_CHARS + 100))
        source["segments"][0]["uncertain"] = False
        value = content()
        value["speaker_summaries"][0].update(speaker="S1", speaker_name="说话人 1")
        invalid = json.loads(json.dumps(value))
        invalid["speaker_summaries"][0]["speaker"] = "private-person-sentinel"
        with tempfile.TemporaryDirectory() as folder:
            responses = [FakeResponse(response_payload(item)) for item in (value, value, invalid)]
            with mock.patch("meeting_tool.summarization.request.urlopen", side_effect=responses) as send:
                with self.assertRaises(SummaryError) as caught:
                    SummaryService("mock-key").generate(source, Path(folder))
            self.assertEqual(send.call_count, 3)
            self.assertIn("第 1 轮合并（1/1）", str(caught.exception))
            self.assertIn("格式无效", str(caught.exception))
            self.assertNotIn("private-person-sentinel", str(caught.exception))
            self.assertEqual(len(list((Path(folder) / ".ai_summary_cache").glob("*.json"))), 2)

    def test_request_uses_official_model_json_non_thinking_and_no_audio(self):
        client = DeepSeekChatClient("mock-key")
        with mock.patch("meeting_tool.summarization.request.urlopen", return_value=FakeResponse(response_payload())) as send:
            result = client.complete([{"role": "user", "content": "test json"}])
        req = send.call_args.args[0]
        payload = json.loads(req.data)
        self.assertEqual(req.full_url, "https://api.deepseek.com/chat/completions")
        self.assertEqual(payload["model"], "deepseek-flash")
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertEqual(payload["thinking"], {"type": "disabled"})
        self.assertNotIn("mock-key", req.data.decode("utf-8"))
        self.assertEqual(result["usage"]["total_tokens"], 20)

    def test_truncated_response_is_rejected_despite_valid_json(self):
        with mock.patch("meeting_tool.summarization.request.urlopen", return_value=FakeResponse(response_payload(finish="length"))):
            with self.assertRaises(SummaryError) as caught:
                DeepSeekChatClient("mock-key").complete([])
        self.assertIn("截断", str(caught.exception))

    def test_malformed_json_response_is_not_retried_and_never_leaks_body(self):
        for raw in (b'{"choices":[null]}', b'{"choices":[{"finish_reason":"stop","message":{"content":"sk-mock-secret"}}]}', b'bad sk-mock-secret'):
            with self.subTest(raw=raw):
                with mock.patch("meeting_tool.summarization.request.urlopen", return_value=FakeResponse(raw)) as send:
                    with self.assertRaises(SummaryError) as caught:
                        DeepSeekChatClient("sk-mock-secret").complete([])
                self.assertNotIn("sk-mock", str(caught.exception))
                self.assertEqual(send.call_count, 1)

    def test_http_and_timeout_errors_do_not_leak_key_and_are_not_retried(self):
        failures = [TimeoutError("mock-key"), error.HTTPError("https://fake/?mock-key", 401, "mock-key", {}, io.BytesIO(b"mock-key"))]
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                with mock.patch("meeting_tool.summarization.request.urlopen", side_effect=failure) as send:
                    with self.assertRaises(SummaryError) as caught:
                        DeepSeekChatClient("mock-key").complete([])
                self.assertNotIn("mock-key", str(caught.exception))
                self.assertEqual(send.call_count, 1)


if __name__ == "__main__":
    unittest.main()
