"""Bounded DeepSeek Responses client with a fixed, speaker-keyed JSON Schema.

No request is retried here. Failed completed outputs can be inspected locally
through the exception's candidate; credentials and HTTP headers are excluded.
"""
from __future__ import annotations

import http.client
import json
import re
import socket
from urllib import error, request

RESPONSES_URL = "https://api.deepseek.com/responses"
SUMMARY_PROTOCOL = "deepseek-responses-summary-map-v1"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_CANDIDATE_BYTES = 512 * 1024
MAX_CONTENT_CHARS = 18_000
ROOT_FIELDS = {"overview", "topics", "decisions", "open_questions", "todos", "speaker_summaries"}
PERSON_FIELDS = {"summary", "points", "commitments", "evidence"}


class SummaryClientError(RuntimeError):
    """Safe error text plus a bounded local-only candidate and diagnostics."""

    def __init__(self, category: str, message: str, *, candidate=None, diagnostics=None,
                 response_status=None, usage=None):
        super().__init__(message)
        self.category = category
        self.candidate = candidate
        self.diagnostics = {"protocol": SUMMARY_PROTOCOL, "category": category, **(diagnostics or {})}
        self.response_status = response_status
        self.usage = dict(usage or {})


def _roster(speakers: dict) -> dict[str, str]:
    if (not isinstance(speakers, dict) or not 1 <= len(speakers) <= 21
            or any(not isinstance(key, str) or len(key) > 40 or not re.fullmatch(r"S[0-9]+|unassigned", key)
                   or not isinstance(name, str) or not name.strip() or len(name) > 200
                   or key == "unassigned" and name != "归属待核"
                   for key, name in speakers.items())):
        raise SummaryClientError("invalid_roster", "总结的人物清单无效，未发送 API 请求。")
    return dict(speakers)


def _object_schema(properties: dict) -> dict:
    return {"type": "object", "properties": properties, "required": list(properties),
            "additionalProperties": False}


def build_summary_schema(speakers: dict) -> dict:
    """Use ID keys chosen by the application; the model never writes IDs/names."""
    speakers = _roster(speakers)

    def strings():
        return {"type": "array", "items": {"type": "string"}}

    people = {}
    for identifier, name in speakers.items():
        person = _object_schema({"summary": {"type": "string"}, "points": strings(),
                                 "commitments": strings(), "evidence": {"type": "string"}})
        person["description"] = "该人物的现有标签：" + name
        people[identifier] = person
    return _object_schema({
        "overview": {"type": "string"},
        "topics": {"type": "array", "items": _object_schema({
            "title": {"type": "string"}, "summary": {"type": "string"}})},
        "decisions": strings(), "open_questions": strings(),
        "todos": {"type": "array", "items": _object_schema({
            "task": {"type": "string"}, "owner": {"type": "string"},
            "deadline": {"type": "string"}, "evidence": {"type": "string"}})},
        "speaker_summaries": _object_schema(people),
    })


def _bounded_candidate(value, secret: str = ""):
    """Keep completed text only, never envelope/header details or credentials."""
    try:
        encoded = json.dumps(value, ensure_ascii=False)
        if len(encoded.encode("utf-8")) > MAX_CANDIDATE_BYTES:
            return None
        if secret:
            # Replace escaped JSON representation as well as plain text.
            escaped = json.dumps(secret, ensure_ascii=False)[1:-1]
            encoded = encoded.replace(escaped, "[已隐藏]")
        return json.loads(encoded)
    except (TypeError, ValueError, UnicodeError):
        return None


def _strict_json_loads(text):
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def invalid_constant(_value):
        raise ValueError("non-JSON numeric constant")

    return json.loads(text, object_pairs_hook=pairs, parse_constant=invalid_constant)


def decode_summary_content(candidate, *, speakers: dict) -> dict:
    """Validate a completed wire object and map it to the existing UI schema.

    This function makes no network calls and is also suitable for rechecking a
    quarantined candidate after a parser fix. It does not invent missing text.
    """
    speakers = _roster(speakers)

    def fail(category, location, *, missing=(), extra=0):
        diagnostics = {"location": location}
        if missing:
            # Only application-owned field names are included, never unknown
            # model keys that may themselves contain private source material.
            diagnostics["missing_fields"] = sorted(missing)
        if extra:
            diagnostics["unexpected_field_count"] = extra
        raise SummaryClientError(category, "AI 返回的总结结构不符合约定，原有总结仍保留。",
                                 candidate=_bounded_candidate(candidate), diagnostics=diagnostics,
                                 response_status="completed")

    def obj(value, fields, location):
        if not isinstance(value, dict):
            fail("object_type", location)
        missing, extra = set(fields) - set(value), set(value) - set(fields)
        if missing or extra:
            fail("field_mismatch", location, missing=missing, extra=len(extra))

    def text(value, limit, location, *, empty=False):
        if not isinstance(value, str):
            fail("text_type", location)
        normalized = value.replace("\r\n", "\n").replace("\r", "\n").strip()
        if len(normalized) > limit or not empty and not normalized:
            fail("text_bounds", location)
        return normalized

    def rows(value, limit, location):
        if not isinstance(value, list) or len(value) > limit:
            fail("list_bounds", location)
        return value

    def strings(value, count, limit, location):
        return [text(item, limit, location) for item in rows(value, count, location)]

    obj(candidate, ROOT_FIELDS, "root")
    output = {"overview": text(candidate["overview"], 3500, "overview"),
              "topics": [], "decisions": strings(candidate["decisions"], 100, 1500, "decisions"),
              "open_questions": strings(candidate["open_questions"], 100, 1500, "open_questions"),
              "todos": [], "speaker_summaries": []}
    for topic in rows(candidate["topics"], 60, "topics"):
        obj(topic, {"title", "summary"}, "topics.item")
        output["topics"].append({"title": text(topic["title"], 200, "topics.title"),
                                 "summary": text(topic["summary"], 2000, "topics.summary")})
    for todo in rows(candidate["todos"], 100, "todos"):
        obj(todo, {"task", "owner", "deadline", "evidence"}, "todos.item")
        output["todos"].append({"task": text(todo["task"], 1600, "todos.task"),
                                "owner": text(todo["owner"], 200, "todos.owner", empty=True) or "未明确",
                                "deadline": text(todo["deadline"], 200, "todos.deadline", empty=True) or "未明确",
                                "evidence": text(todo["evidence"], 1000, "todos.evidence")})
    obj(candidate["speaker_summaries"], speakers, "speaker_summaries")
    for identifier, name in speakers.items():
        person = candidate["speaker_summaries"][identifier]
        obj(person, PERSON_FIELDS, "speaker_summaries.item")
        commitments = strings(person["commitments"], 8, 800, "speaker_summaries.commitments")
        if identifier == "unassigned" and commitments:
            fail("unassigned_commitment", "speaker_summaries.commitments")
        output["speaker_summaries"].append({
            "speaker": identifier, "speaker_name": name,
            "summary": text(person["summary"], 1600, "speaker_summaries.summary"),
            "points": strings(person["points"], 8, 800, "speaker_summaries.points"),
            "commitments": commitments,
            "evidence": text(person["evidence"], 1200, "speaker_summaries.evidence"),
        })
    if len(json.dumps(output, ensure_ascii=False, sort_keys=True, separators=(",", ":"))) > MAX_CONTENT_CHARS:
        fail("content_bounds", "root")
    return output


def _usage(value) -> dict:
    value = value if isinstance(value, dict) else {}
    return {target: number if isinstance(number := value.get(source), int)
            and not isinstance(number, bool) and 0 <= number <= 10**10 else 0
            for source, target in (("input_tokens", "prompt_tokens"),
                                   ("output_tokens", "completion_tokens"), ("total_tokens", "total_tokens"))}


class DeepSeekResponsesClient:
    def __init__(self, api_key: str, *, model: str = "deepseek-flash", timeout: float = 180):
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("请填写 DeepSeek API Key。")
        if model not in {"deepseek-flash", "deepseek-v4-pro"}:
            raise ValueError("请选择支持的 DeepSeek 总结模型。")
        self._api_key, self.model, self.timeout = api_key.strip(), model, timeout

    def complete(self, messages: list[dict], *, speakers: dict) -> dict:
        speakers = _roster(speakers)
        body = json.dumps({
            "model": self.model, "input": messages, "stream": False, "store": False,
            "reasoning": {"effort": "none"}, "temperature": 0.2, "max_output_tokens": 6000,
            "text": {"format": {"type": "json_schema", "name": "meeting_summary",
                                 "schema": build_summary_schema(speakers)}},
        }, ensure_ascii=False).encode("utf-8")
        req = request.Request(RESPONSES_URL, data=body, headers={
            "Authorization": "Bearer " + self._api_key, "Content-Type": "application/json",
            "Accept": "application/json", "User-Agent": "TingjiMeetingWorkbench/2.1"}, method="POST")
        try:
            with request.urlopen(req, timeout=self.timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except error.HTTPError as exc:
            status = exc.code
            try:
                exc.close()
            except OSError:
                pass
            description = {400: "结构化总结请求未被接受", 401: "API Key 无效或已失效", 402: "账户余额不足",
                           403: "账户没有访问权限", 429: "请求过多或服务限流", 500: "服务端出错",
                           503: "服务繁忙"}.get(status, "接口暂时不可用")
            raise SummaryClientError("http_error", f"DeepSeek HTTP {status}：{description}。未自动重试，原有总结仍保留。",
                                     diagnostics={"http_status": status}) from None
        except (error.URLError, TimeoutError, socket.timeout, OSError, http.client.HTTPException):
            raise SummaryClientError("transport_error", "AI 总结请求中断或等待超时，服务端可能已计费；未自动重试，原有总结仍保留。") from None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise SummaryClientError("response_bounds", "AI 接口返回数据过大，未自动重试，原有总结仍保留。")
        try:
            payload = _strict_json_loads(raw.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError
        except (ValueError, UnicodeError):
            raise SummaryClientError("response_json", "AI 接口未返回可解析的响应，未自动重试，原有总结仍保留。") from None
        if payload.get("status") != "completed" or payload.get("error") or payload.get("incomplete_details"):
            raise SummaryClientError("response_incomplete", "AI 总结未完整生成或被中止，未自动重试，原有总结仍保留。")
        output = payload.get("output")
        if not isinstance(output, list) or not 1 <= len(output) <= 32:
            raise SummaryClientError("response_output", "AI 接口未返回可用的总结正文，原有总结仍保留。")
        texts = []
        for item in output:
            if isinstance(item, dict) and item.get("type") == "reasoning" and item.get("status") in (None, "completed"):
                # Responses may include a separate reasoning item. It is not
                # summary text, must not enter the report/candidate, and is not
                # needed to reconstruct the public assistant answer.
                continue
            if (not isinstance(item, dict) or item.get("type") != "message"
                    or item.get("role") != "assistant" or item.get("status") != "completed"
                    or not isinstance(item.get("content"), list) or not item["content"]):
                raise SummaryClientError("unexpected_output", "AI 接口返回了非总结内容或未完成内容，原有总结仍保留。")
            for part in item["content"]:
                if not isinstance(part, dict) or part.get("type") != "output_text" or not isinstance(part.get("text"), str):
                    raise SummaryClientError("unexpected_output", "AI 接口未返回完整的文本总结，原有总结仍保留。")
                texts.append(part["text"])
        text = "".join(texts)
        if not text.strip():
            raise SummaryClientError("response_output", "AI 接口未返回可用的总结正文，原有总结仍保留。")
        try:
            candidate = _strict_json_loads(text)
        except (ValueError, TypeError):
            raise SummaryClientError("content_json", "AI 总结正文不是完整的 JSON，原有总结仍保留。",
                                     candidate=_bounded_candidate(text, self._api_key),
                                     response_status="completed", usage=_usage(payload.get("usage"))) from None
        try:
            content = decode_summary_content(candidate, speakers=speakers)
        except SummaryClientError as exc:
            exc.candidate = _bounded_candidate(candidate, self._api_key)
            exc.response_status = "completed"
            exc.usage = _usage(payload.get("usage"))
            raise
        request_id = payload.get("id", "")
        request_id = request_id if isinstance(request_id, str) else ""
        # A model echoing the current key must not cause it to be written into
        # successful caches, final reports, or a later validation quarantine.
        content = _bounded_candidate(content, self._api_key)
        if not isinstance(content, dict):
            raise SummaryClientError("content_bounds", "AI 总结内容无法安全保存，原有总结仍保留。")
        return {"content": content, "usage": _usage(payload.get("usage")),
                "request_id": request_id.replace(self._api_key, "[已隐藏]")[:250]}
