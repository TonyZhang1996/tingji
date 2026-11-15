"""Small DeepSeek ASR client; API credentials stay in memory."""
from __future__ import annotations

import json
import http.client
from pathlib import Path
import socket
import threading
from urllib import error, request
import uuid

from .audio import CancelledError, wav_duration

ASR_URL = "https://api.deepseek.com/v1/tools/asr"


class ApiError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, retryable: bool = False,
                 ambiguous: bool = False, retry_after: float | None = None):
        super().__init__(message)
        self.status = status
        self.retryable = retryable
        self.ambiguous = ambiguous
        self.retry_after = retry_after


class DeepSeekASR:
    def __init__(self, api_key: str, *, endpoint: str = ASR_URL, timeout: float = 120,
                 max_attempts: int = 3, log=None):
        if not api_key.strip():
            raise ValueError("请填写 DeepSeek API Key。")
        self._api_key = api_key.strip()
        self.endpoint = endpoint
        self.timeout = timeout
        self.max_attempts = max(1, min(int(max_attempts), 5))
        self.log = log or (lambda _message: None)

    def transcribe(self, wav_path: Path, cancel_event: threading.Event | None = None) -> dict:
        """Retry explicit server rejections, but do not silently retry ambiguous timeouts."""
        wav_path = Path(wav_path)
        if wav_path.stat().st_size > 8 * 1024 * 1024:
            raise ValueError("音频分段超过 DeepSeek 的 8 MiB 限制。")
        if wav_duration(wav_path) > 120.0:
            raise ValueError("音频分段超过 DeepSeek 的 120 秒限制。")
        boundary = "----DeepSeekMeeting" + uuid.uuid4().hex
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"speech.wav\"\r\n"
                "Content-Type: audio/wav\r\n\r\n").encode("ascii")
        body += wav_path.read_bytes() + f"\r\n--{boundary}--\r\n".encode("ascii")
        for attempt in range(self.max_attempts):
            if cancel_event is not None and cancel_event.is_set():
                raise CancelledError("已停止；未发送新的转写请求。")
            req = request.Request(self.endpoint, data=body, headers={
                "Authorization": "Bearer " + self._api_key,
                "Content-Type": "multipart/form-data; boundary=" + boundary,
                "Accept": "application/json", "User-Agent": "DeepSeekMeetingTool/1.0",
            }, method="POST")
            try:
                with request.urlopen(req, timeout=self.timeout) as response:
                    raw = response.read(16 * 1024 * 1024 + 1)
                if len(raw) > 16 * 1024 * 1024:
                    raise ApiError("接口返回数据过大，结果不明；可能已计费，请勿盲目重试。", ambiguous=True)
                try:
                    result = json.loads(raw.decode("utf-8"))
                except (ValueError, UnicodeError):
                    raise ApiError("接口返回了无法读取的结果；可能已计费，已暂停该片段。", ambiguous=True) from None
                if not isinstance(result, dict) or not isinstance(result.get("text"), str):
                    raise ApiError("接口未返回有效转写文字；可能已计费，已暂停该片段。", ambiguous=True)
                return {"text": result["text"], "request_id": str(result.get("request_id", "")),
                        "duration_ms": result.get("duration_ms")}
            except error.HTTPError as exc:
                status = exc.code
                try:
                    exc.close()
                except OSError:
                    pass
                descriptions = {400: "音频格式或请求不符合要求", 401: "API Key 无效或已失效",
                                402: "账户余额不足", 403: "该账户没有访问权限", 408: "音频上传超时",
                                413: "音频分段过大", 429: "请求过多或服务限流", 500: "服务端出错",
                                503: "服务繁忙", 504: "服务端识别超时"}
                retryable = status == 429 or 500 <= status < 600
                # A server-side timeout may have completed/charged; require an explicit retry.
                ambiguous = status in (408, 504)
                retryable = retryable and not ambiguous
                api_error = ApiError(f"DeepSeek HTTP {status}：{descriptions.get(status, '接口拒绝请求')}。",
                                     status=status, retryable=retryable, ambiguous=ambiguous)
                retry_after = None
                try:
                    retry_after = float(exc.headers.get("Retry-After", ""))
                except (TypeError, ValueError):
                    pass
                if not retryable or attempt + 1 >= self.max_attempts:
                    if ambiguous:
                        api_error.args = (str(api_error) + " 可能已计费，已暂停自动重试。",)
                    raise api_error from None
                delay = min(30, max(2 ** (attempt + 1), retry_after or 0))
                self.log(f"接口暂时不可用（HTTP {status}），{delay:g} 秒后进行第 {attempt + 2} 次尝试。")
                if cancel_event is not None:
                    if cancel_event.wait(delay):
                        raise CancelledError("已停止，未进行下一次重试。")
                else:
                    threading.Event().wait(delay)
            except (error.URLError, TimeoutError, socket.timeout, OSError, http.client.HTTPException) as exc:
                # A failed response does not prove the service never received the upload.
                raise ApiError("网络中断或等待结果超时。服务端可能已完成并计费；该片段记为结果不明，默认不重复提交。",
                               ambiguous=True) from None
        raise AssertionError("unreachable")
