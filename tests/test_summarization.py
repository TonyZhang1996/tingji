from __future__ import annotations

import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock
from urllib import error

from meeting_tool.audio import CancelledError
from meeting_tool.summarization import (CHUNK_CHARS, DEFAULT_MODEL, DeepSeekChatClient,
                                        SummaryError, SummaryService, _transcript_chunks,
                                        render_markdown, validate_content)


def content(overview="讨论了项目计划。", *, todo=True):
    return {"overview": overview, "topics": [{"title": "项目计划", "summary": "讨论了检查数据的方式。"}],
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
            self.assertIn("归属待核", material)
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


class SummaryHttpTests(unittest.TestCase):
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
