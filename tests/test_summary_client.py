"""Official Responses REST envelopes and strict meeting schemas; no real API."""
from __future__ import annotations

import io
import json
import unittest
from unittest.mock import patch
from urllib import error

from meeting_tool.summary_client import (DeepSeekResponsesClient, MAX_RESPONSE_BYTES,
                                         SUMMARY_PROTOCOL, SummaryClientError,
                                         build_summary_schema, decode_summary_content)


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def wire_content(speakers=None):
    speakers = speakers or {"S1": "甲", "S2": "乙"}
    return {"overview": "讨论本周的需求和联调安排。",
            "topics": [{"title": "交付范围", "summary": "按已确认的需求完成联调。"}],
            "decisions": ["先核对需求。"], "open_questions": [],
            "todos": [{"task": "核对需求说明", "owner": "", "deadline": "明天",
                       "evidence": "00:01:00—00:01:20"}],
            "speaker_summaries": {identifier: {
                "summary": "讨论需求确认和联调安排。", "points": ["先核对需求。"],
                "commitments": [], "evidence": "00:01:00—00:01:20"}
                for identifier in speakers}}


def envelope(value=None, *, status="completed", text=None):
    text = json.dumps(value if value is not None else wire_content(), ensure_ascii=False) if text is None else text
    return {"id": "response-fixture", "object": "response", "status": status,
            "error": None, "incomplete_details": None,
            "output": [{"type": "message", "role": "assistant", "status": "completed",
                        "content": [{"type": "output_text", "text": text}]}],
            "usage": {"input_tokens": 12, "output_tokens": 8, "total_tokens": 20}}


def response(payload):
    return FakeResponse(json.dumps(payload, ensure_ascii=False).encode("utf-8"))


class ResponsesSummaryTests(unittest.TestCase):
    def setUp(self):
        self.client = DeepSeekResponsesClient("mock-private-api-key")
        self.speakers = {"S1": "同名", "S2": "同名"}
        self.messages = [{"role": "system", "content": "仅整理会议内容。"},
                         {"role": "user", "content": "两位人物的虚构发言。"}]

    def complete(self, payload, *, speakers=None):
        with patch("meeting_tool.summary_client.request.urlopen", return_value=response(payload)) as send:
            result = self.client.complete(self.messages, speakers=speakers or self.speakers)
        return result, send

    def test_official_request_uses_schema_and_program_owned_identity(self):
        result, send = self.complete(envelope(wire_content(self.speakers)))
        self.assertEqual(send.call_count, 1)
        req = send.call_args.args[0]
        body = json.loads(req.data)
        self.assertEqual(req.full_url, "https://api.deepseek.com/responses")
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.get_header("Authorization"), "Bearer mock-private-api-key")
        self.assertNotIn("mock-private-api-key", req.data.decode("utf-8"))
        self.assertEqual(body["input"], self.messages)
        self.assertEqual(body["model"], "deepseek-flash")
        self.assertEqual(body["reasoning"], {"effort": "none"})
        self.assertEqual(body["max_output_tokens"], 6000)
        self.assertIs(body["stream"], False)
        self.assertIs(body["store"], False)
        self.assertNotIn("tools", body)
        self.assertNotIn("response_format", body)
        format = body["text"]["format"]
        self.assertEqual(format["type"], "json_schema")
        self.assertEqual(format["name"], "meeting_summary")
        schema = format["schema"]
        self.assertIs(schema["additionalProperties"], False)
        self.assertEqual(set(schema["required"]), set(wire_content()))
        people = schema["properties"]["speaker_summaries"]
        self.assertEqual(people["required"], ["S1", "S2"])
        self.assertIs(people["additionalProperties"], False)
        for person in people["properties"].values():
            self.assertEqual(set(person["required"]), {"summary", "points", "commitments", "evidence"})
            self.assertNotIn("speaker", person["properties"])
            self.assertNotIn("speaker_name", person["properties"])
        self.assertEqual([p["speaker"] for p in result["content"]["speaker_summaries"]], ["S1", "S2"])
        self.assertEqual([p["speaker_name"] for p in result["content"]["speaker_summaries"]], ["同名", "同名"])
        self.assertEqual(result["content"]["todos"][0]["owner"], "未明确")
        self.assertEqual(result["usage"], {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20})

    def test_output_text_parts_are_joined_without_sdk_aggregate(self):
        payload = envelope(wire_content(self.speakers))
        text = payload["output"][0]["content"][0]["text"]
        split = len(text) // 2
        payload["output"][0]["content"] = [{"type": "output_text", "text": text[:split]},
                                                {"type": "output_text", "text": text[split:]}]
        payload["output_text"] = "must-not-be-used"
        result, _ = self.complete(payload)
        self.assertEqual(result["content"]["overview"], wire_content()["overview"])

    def test_reasoning_is_ignored_without_entering_report_or_local_candidate(self):
        payload = envelope(wire_content(self.speakers))
        payload["output"].insert(0, {"type": "reasoning", "content": [
            {"type": "reasoning_text", "text": "private-reasoning-sentinel"}]})
        result, _ = self.complete(payload)
        self.assertNotIn("private-reasoning-sentinel", json.dumps(result))
        payload["output"] = payload["output"][:1]
        with patch("meeting_tool.summary_client.request.urlopen", return_value=response(payload)):
            with self.assertRaises(SummaryClientError) as caught:
                self.client.complete(self.messages, speakers=self.speakers)
        self.assertIsNone(caught.exception.candidate)

    def test_missing_extra_and_wrong_structures_retain_candidate_without_private_keys_in_diagnostics(self):
        for mode in ("missing_root", "extra_root", "missing_person", "extra_person", "wrong_map", "wrapper"):
            with self.subTest(mode=mode):
                value = wire_content(self.speakers)
                if mode == "missing_root":
                    del value["topics"]
                elif mode == "extra_root":
                    value["private-extra-key-sentinel"] = "private body"
                elif mode == "missing_person":
                    del value["speaker_summaries"]["S1"]["evidence"]
                elif mode == "extra_person":
                    value["speaker_summaries"]["private-person-key-sentinel"] = value["speaker_summaries"]["S1"]
                elif mode == "wrong_map":
                    value["speaker_summaries"] = list(value["speaker_summaries"].values())
                else:
                    value = {"content": value}
                with patch("meeting_tool.summary_client.request.urlopen", return_value=response(envelope(value))) as send:
                    with self.assertRaises(SummaryClientError) as caught:
                        self.client.complete(self.messages, speakers=self.speakers)
                exc = caught.exception
                self.assertEqual(send.call_count, 1)
                self.assertEqual(exc.candidate, value)
                self.assertEqual(exc.response_status, "completed")
                self.assertEqual(exc.usage["total_tokens"], 20)
                self.assertEqual(exc.diagnostics["protocol"], SUMMARY_PROTOCOL)
                for secret in ("private-extra-key-sentinel", "private-person-key-sentinel", "private body"):
                    self.assertNotIn(secret, str(exc))
                    self.assertNotIn(secret, json.dumps(exc.diagnostics))

    def test_missing_person_is_not_guessed_from_same_name_or_position(self):
        value = wire_content(self.speakers)
        del value["speaker_summaries"]["S2"]
        with self.assertRaises(SummaryClientError) as caught:
            decode_summary_content(value, speakers=self.speakers)
        self.assertEqual(caught.exception.category, "field_mismatch")
        self.assertEqual(caught.exception.diagnostics["missing_fields"], ["S2"])

    def test_completed_invalid_json_and_duplicate_keys_are_local_candidates(self):
        for text in ("{broken-json", '{"speaker_summaries":{"S1":{},"S1":{}}}'):
            with self.subTest(text=text):
                with patch("meeting_tool.summary_client.request.urlopen", return_value=response(envelope(text=text))) as send:
                    with self.assertRaises(SummaryClientError) as caught:
                        self.client.complete(self.messages, speakers=self.speakers)
                self.assertEqual(send.call_count, 1)
                self.assertEqual(caught.exception.category, "content_json")
                self.assertEqual(caught.exception.candidate, text)
                self.assertEqual(caught.exception.response_status, "completed")

    def test_incomplete_refusal_tool_calls_and_unfinished_messages_cannot_be_promoted(self):
        for mode in ("incomplete", "failed", "refusal", "tool", "unfinished", "aggregate_only"):
            with self.subTest(mode=mode):
                payload = envelope(wire_content(self.speakers))
                if mode in {"incomplete", "failed"}:
                    payload["status"] = mode
                elif mode == "refusal":
                    payload["output"][0]["content"] = [{"type": "refusal", "refusal": "private reason"}]
                elif mode == "tool":
                    payload["output"].append({"type": "function_call", "arguments": "private data"})
                elif mode == "unfinished":
                    payload["output"][0]["status"] = "in_progress"
                else:
                    payload["output_text"] = payload.pop("output")[0]["content"][0]["text"]
                with patch("meeting_tool.summary_client.request.urlopen", return_value=response(payload)) as send:
                    with self.assertRaises(SummaryClientError) as caught:
                        self.client.complete(self.messages, speakers=self.speakers)
                self.assertEqual(send.call_count, 1)
                self.assertIsNone(caught.exception.candidate)
                self.assertNotEqual(caught.exception.response_status, "completed")
                self.assertNotIn("private", str(caught.exception))

    def test_http_failures_and_timeouts_never_retry_or_reveal_credentials(self):
        for status in (401, 402, 429):
            failure = error.HTTPError("https://fake/?mock-private-api-key", status, "mock-private-api-key", {}, io.BytesIO(b"private body"))
            with self.subTest(status=status):
                with patch("meeting_tool.summary_client.request.urlopen", side_effect=failure) as send:
                    with self.assertRaises(SummaryClientError) as caught:
                        self.client.complete(self.messages, speakers=self.speakers)
                self.assertEqual(send.call_count, 1)
                self.assertEqual(caught.exception.diagnostics["http_status"], status)
                self.assertIsNone(caught.exception.candidate)
                self.assertNotIn("mock-private-api-key", str(caught.exception))
        with patch("meeting_tool.summary_client.request.urlopen", side_effect=TimeoutError("mock-private-api-key")) as send:
            with self.assertRaises(SummaryClientError) as caught:
                self.client.complete(self.messages, speakers=self.speakers)
        self.assertEqual(send.call_count, 1)
        self.assertIsNone(caught.exception.candidate)
        self.assertIn("可能已计费", str(caught.exception))
        self.assertNotIn("mock-private-api-key", str(caught.exception))

    def test_candidate_never_contains_current_key_even_in_unknown_model_fields(self):
        value = wire_content(self.speakers)
        value["mock-private-api-key"] = "quoted mock-private-api-key"
        with patch("meeting_tool.summary_client.request.urlopen", return_value=response(envelope(value))):
            with self.assertRaises(SummaryClientError) as caught:
                self.client.complete(self.messages, speakers=self.speakers)
        self.assertNotIn("mock-private-api-key", json.dumps(caught.exception.candidate))
        self.assertNotIn("mock-private-api-key", str(caught.exception))
        self.assertNotIn("mock-private-api-key", json.dumps(caught.exception.diagnostics))

    def test_successful_content_and_program_owned_names_do_not_persist_current_key(self):
        people = {"S1": "mock-private-api-key"}
        value = wire_content(people)
        value["overview"] = "quoted mock-private-api-key"
        value["speaker_summaries"]["S1"]["points"] = ["quoted mock-private-api-key"]
        result, _ = self.complete(envelope(value), speakers=people)
        self.assertNotIn("mock-private-api-key", json.dumps(result))
        self.assertIn("[已隐藏]", result["content"]["overview"])

    def test_unassigned_cannot_acquire_a_commitment(self):
        people = {"unassigned": "归属待核"}
        value = wire_content(people)
        value["speaker_summaries"]["unassigned"]["commitments"] = ["某人承诺提交"]
        with self.assertRaises(SummaryClientError) as caught:
            decode_summary_content(value, speakers=people)
        self.assertEqual(caught.exception.category, "unassigned_commitment")

    def test_invalid_usage_is_not_copied_as_token_counts(self):
        payload = envelope(wire_content(self.speakers))
        payload["usage"] = {"input_tokens": True, "output_tokens": "8", "total_tokens": -1}
        result, _ = self.complete(payload)
        self.assertEqual(result["usage"], {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0})

    def test_invalid_roster_fails_before_network_request(self):
        for people in ({}, {"unknown": "甲"}, {"S1": ""}, {"unassigned": "冒充姓名"}):
            with self.subTest(people=people):
                with patch("meeting_tool.summary_client.request.urlopen") as send:
                    with self.assertRaises(SummaryClientError):
                        self.client.complete(self.messages, speakers=people)
                send.assert_not_called()

    def test_response_and_candidate_size_bounds(self):
        with patch("meeting_tool.summary_client.request.urlopen", return_value=FakeResponse(b"x" * (MAX_RESPONSE_BYTES + 1))) as send:
            with self.assertRaises(SummaryClientError) as caught:
                self.client.complete(self.messages, speakers=self.speakers)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(caught.exception.category, "response_bounds")
        value = wire_content(self.speakers)
        value["unexpected"] = "甲" * 200_000
        with patch("meeting_tool.summary_client.request.urlopen", return_value=response(envelope(value))):
            with self.assertRaises(SummaryClientError) as caught:
                self.client.complete(self.messages, speakers=self.speakers)
        self.assertIsNone(caught.exception.candidate)


if __name__ == "__main__":
    unittest.main()
