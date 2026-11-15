"""Desktop presentation layer. Paid engine records remain authoritative and untouched."""
from __future__ import annotations

from collections import OrderedDict, deque
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import threading
import time
from typing import Callable

from .audio import probe_audio
from .engine import DIARIZATION_VERSION, WORKFLOW_VERSION, JobLock, MeetingJob, RunConfig, job_directory
from .exports import atomic_json, export_transcript
from .summarization import (DEFAULT_MODEL, PROMPT_VERSION, SummaryService,
                            render_markdown, validate_content)

VERSION = "2.1"
MEDIA_EXTENSIONS = {".wav", ".m4a", ".mp3", ".mp4", ".aac", ".ogg", ".opus", ".flac", ".wma", ".mkv", ".mov", ".amr"}
EXPORT_FILES = {"txt": "完整转写.txt", "md": "完整转写.md", "json": "完整转写.json", "csv": "分段明细.csv", "srt": "片段时间轴_非逐字字幕.srt"}
_JSON_CACHE = OrderedDict()
_JSON_CACHE_LOCK = threading.RLock()
_JSON_CACHE_MAX_BYTES = 32 * 1024 * 1024


def read_json(path: Path, default=None):
    try:
        path = Path(path)
        stat = path.stat()
        if stat.st_size > 64 * 1024 * 1024:
            return default
        key = str(path.resolve())
        signature = (stat.st_mtime_ns, stat.st_size)
        with _JSON_CACHE_LOCK:
            cached = _JSON_CACHE.get(key)
            if cached and cached[0] == signature:
                _JSON_CACHE.move_to_end(key)
                return cached[1]
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        if stat.st_size <= _JSON_CACHE_MAX_BYTES:
            with _JSON_CACHE_LOCK:
                _JSON_CACHE[key] = (signature, value, stat.st_size)
                _JSON_CACHE.move_to_end(key)
                while len(_JSON_CACHE) > 64 or sum(row[2] for row in _JSON_CACHE.values()) > _JSON_CACHE_MAX_BYTES:
                    _JSON_CACHE.popitem(last=False)
        return value
    except (OSError, ValueError, TypeError):
        return default


def segment_id(turn: dict) -> str:
    if turn.get("id"):
        return str(turn["id"])
    identity = {key: turn[key] for key in ("start", "end", "speaker", "uncertain") if key in turn}
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode("utf-8")).hexdigest()[:20]


class MeetingController:
    def __init__(self, app_dir: Path, *, settings_path: Path | None = None, job_factory=MeetingJob,
                 dialog_picker: Callable | None = None, opener: Callable | None = None,
                 summary_factory=SummaryService):
        self.app_dir = Path(app_dir).resolve()
        self.settings_path = Path(settings_path or self.app_dir / "settings.json")
        self.job_factory = job_factory
        self.summary_factory = summary_factory
        self.dialog_picker = dialog_picker
        self.opener = opener or self._system_open
        self._lock = threading.RLock()
        self._picker_lock = threading.Lock()
        self._source = None
        self._selected = None
        self._jobs: dict[str, Path] = {}
        self._media: dict[str, Path] = {}
        self._summary_cache = {}
        self._ai_summaries = {}
        self._last_scan = -float("inf")
        self._last_scan_root = None
        self._logs = deque(maxlen=250)
        self._worker: threading.Thread | None = None
        self._cancel = threading.Event()
        self._task = {"running": False, "stage": "idle", "current": 0, "total": 0,
                      "message": "选择录音，或打开一个历史任务。", "cancel_requested": False}
        defaults = {"num_speakers": 0, "output_root": str(self.app_dir / "转写结果"),
                    "hourly_price": 0.39, "max_workers": 2, "retry_uncertain": False}
        saved = read_json(self.settings_path, {})
        self.settings = {key: saved.get(key, value) if isinstance(saved, dict) else value for key, value in defaults.items()}
        try:
            self.settings = self._validate_settings(self.settings)
        except ValueError:
            self.settings = defaults
        self._scan_jobs()
        if self._jobs:
            self._select_job_unlocked(next(iter(self._jobs)))

    @staticmethod
    def _system_open(path: Path):
        if os.name != "nt":
            raise RuntimeError("请在 Windows 桌面窗口中打开结果。")
        os.startfile(str(path))

    def _validate_settings(self, data: dict) -> dict:
        allowed = {"num_speakers", "output_root", "hourly_price", "max_workers", "retry_uncertain"}
        if set(data) - allowed:
            raise ValueError("设置中含有不支持的字段。")
        result = dict(getattr(self, "settings", {}))
        result.update(data)
        try:
            count, workers, price = int(result["num_speakers"]), int(result["max_workers"]), float(result["hourly_price"])
        except (KeyError, TypeError, ValueError):
            raise ValueError("请填写有效的人数、并发数和价格。") from None
        if not 0 <= count <= 20 or not 1 <= workers <= 4 or not math.isfinite(price) or not 0 < price < 100:
            raise ValueError("人数应为 0～20，并发应为 1～4，单价应为有效正数。")
        raw_output = str(result.get("output_root", "")).strip()
        if not raw_output:
            raise ValueError("请选择结果保存目录。")
        retry = result.get("retry_uncertain", False)
        if not isinstance(retry, bool):
            raise ValueError("重试选项必须为开或关。")
        return {"num_speakers": count, "max_workers": workers, "hourly_price": price,
                "output_root": str(Path(raw_output).expanduser().resolve()), "retry_uncertain": retry}

    def _ensure_idle(self):
        if self._task["running"]:
            raise ValueError("当前任务仍在运行，请先停止并等待保存完成。")

    def _log(self, message: str):
        with self._lock:
            self._logs.append({"time": datetime.now().strftime("%H:%M:%S"), "message": str(message)[:4000]})

    def _register_job(self, path: Path) -> str:
        path = path.resolve()
        key = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:24]
        self._jobs[key] = path
        return key

    def _scan_jobs(self):
        now = time.monotonic()
        root = Path(self.settings["output_root"])
        if not root.is_dir():
            return
        root_signature = (str(root), root.stat().st_mtime_ns)
        if root_signature == self._last_scan_root and now - self._last_scan < 1:
            return
        self._last_scan = now
        self._last_scan_root = root_signature
        try:
            directories = sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            return
        for path in directories[:1000]:
            if (path / "analysis.json").is_file():
                self._register_job(path)

    def _old_version(self, analysis: dict):
        fingerprint = analysis.get("fingerprint", {})
        return (fingerprint.get("workflow_version") != WORKFLOW_VERSION
                or analysis.get("diarization_version", fingerprint.get("diarization_version")) != DIARIZATION_VERSION)

    def _metadata(self, path: Path):
        analysis = read_json(path / "analysis.json", {})
        checkpoint = read_json(path / "checkpoint.json", {})
        transcript = read_json(path / "完整转写.json", {})
        if not isinstance(analysis, dict):
            analysis = {}
        if not isinstance(checkpoint, dict):
            checkpoint = {}
        if not isinstance(transcript, dict):
            transcript = {}
        return analysis, checkpoint, transcript

    def _job_summary(self, key, path):
        signature = []
        for filename in ("analysis.json", "checkpoint.json", "完整转写.json"):
            try:
                stat = (path / filename).stat()
                signature.append((stat.st_mtime_ns, stat.st_size))
            except OSError:
                signature.append(None)
        signature = tuple(signature)
        cached = self._summary_cache.get(key)
        if cached and cached[0] == signature:
            summary = dict(cached[1])
            if self._task["running"] and key == self._selected:
                summary["status"] = "processing"
            return summary
        analysis, checkpoint, transcript = self._metadata(path)
        fingerprint = analysis.get("fingerprint", {})
        source = str(fingerprint.get("path") or checkpoint.get("metadata", {}).get("audio_path") or "")
        segments = checkpoint.get("segments") or transcript.get("segments") or []
        count = sum(row.get("state") == "done" for row in segments)
        complete = bool(segments) and count == len(segments)
        status = "complete" if complete else "partial" if segments else "analyzed" if analysis else "pending"
        try:
            updated = datetime.fromtimestamp((path / "checkpoint.json" if (path / "checkpoint.json").exists() else path).stat().st_mtime).astimezone().isoformat()
        except OSError:
            updated = datetime.now().astimezone().isoformat()
        summary = {"id": key, "title": Path(source).stem if source else path.name,
                "source_path": source, "duration_seconds": analysis.get("duration_seconds", 0),
                "speaker_count": analysis.get("num_speakers_found", 0), "updated_at": updated,
                "status": status, "old_version": self._old_version(analysis) if analysis else False,
                "done_count": count, "segment_count": len(segments) or len(analysis.get("turns", []))}
        self._summary_cache[key] = (signature, dict(summary))
        if self._task["running"] and key == self._selected:
            summary["status"] = "processing"
        return summary

    def _select_job_unlocked(self, key: str):
        if key not in self._jobs:
            raise ValueError("该历史任务不存在，请刷新任务列表。")
        self._selected = key
        path = self._jobs[key]
        analysis = read_json(path / "analysis.json", {})
        fingerprint = analysis.get("fingerprint", {})
        source_path = fingerprint.get("path")
        if source_path:
            self._source = {**analysis.get("source_info", {}), "path": source_path,
                            "name": Path(source_path).name, "duration_seconds": analysis.get("duration_seconds", 0)}
        self.settings["num_speakers"] = int(analysis.get("num_speakers_requested", fingerprint.get("num_speakers", self.settings["num_speakers"])))
        self.settings["output_root"] = str(path.parent)

    def select_audio(self, path: Path | str):
        source = Path(path).expanduser().resolve()
        with self._lock:
            self._ensure_idle()
        if source.suffix.lower() not in MEDIA_EXTENSIONS:
            raise ValueError("请选择音频或视频文件。")
        info = probe_audio(source)
        with self._lock:
            self._ensure_idle()
            self._source = {**info, "path": str(source), "name": source.name}
            config = self._config("")
            candidate = job_directory(config)
            self._selected = self._register_job(candidate) if (candidate / "analysis.json").is_file() else None
            self._task.update(stage="idle", current=0, total=0, message="录音已选择。可以先免费分析人物。")
            self._log("已选择录音：" + source.name)
        return self.state()

    def register_media(self, path: Path, *, under: Path | None = None):
        path = Path(path).resolve()
        if under is not None and not path.is_relative_to(Path(under).resolve()):
            return None
        if not path.is_file() or path.suffix.lower() not in MEDIA_EXTENSIONS:
            return None
        key = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:24]
        self._media[key] = path
        return "/api/media/" + key

    def media_path(self, key: str):
        with self._lock:
            path = self._media.get(key)
        if path is None or not path.is_file():
            raise FileNotFoundError("音频文件不可用。")
        return path

    @staticmethod
    def _names(path: Path):
        value = read_json(path / "speaker_names.json", {})
        return {str(k): str(v) for k, v in value.items() if re.fullmatch(r"S\d+", str(k)) and isinstance(v, str)} if isinstance(value, dict) else {}

    def _annotations(self, path: Path, analysis: dict, *, strict=False):
        file = path / "review_annotations.json"
        if not file.is_file():
            return {}
        value = read_json(file)
        if (not isinstance(value, dict) or value.get("fingerprint") != analysis.get("fingerprint")
                or not isinstance(value.get("segments"), dict)):
            if strict:
                raise ValueError("校对记录无法验证，请保留该任务目录供检查。")
            return {}
        return value["segments"]

    def _rows(self, analysis, checkpoint, transcript):
        raw = checkpoint.get("segments") or transcript.get("segments")
        if not isinstance(raw, list) or not raw:
            raw = [{**turn, "index": index, "state": "pending", "text": ""} for index, turn in enumerate(analysis.get("turns", []))]
        return [{**row, "id": segment_id(row), "index": row.get("index", index)} for index, row in enumerate(raw)]

    def _overlay(self, rows, annotations, names):
        result = []
        for row in rows:
            copied = dict(row)
            change = annotations.get(row["id"], {})
            if isinstance(change, dict):
                copied.update({key: value for key, value in change.items() if key in ("text", "speaker", "reviewed")})
            copied["reviewed"] = bool(copied.get("reviewed", False))
            copied["edited"] = bool(change)
            copied["machine_uncertain"] = bool(row.get("uncertain", False))
            # A human confirmation overrides the displayed/exported warning,
            # while the original machine ledger remains unchanged on disk.
            copied["uncertain"] = bool(row.get("uncertain", False) and not copied["reviewed"])
            copied["speaker_name"] = names.get(copied.get("speaker"), str(copied.get("speaker", "")).replace("S", "说话人 ", 1))
            result.append(copied)
        return result

    def _selected_payload(self):
        if not self._selected or self._selected not in self._jobs:
            return None
        path = self._jobs[self._selected]
        analysis, checkpoint, transcript = self._metadata(path)
        summary = self._job_summary(self._selected, path)
        rows = self._rows(analysis, checkpoint, transcript)
        names = self._names(path) or transcript.get("speaker_names", {})
        annotations = self._annotations(path, analysis)
        rows = self._overlay(rows, annotations, names)
        snapshot = self._summary_snapshot(path, summary=summary, rows=rows)
        samples = []
        for sample in analysis.get("samples", []):
            sample = dict(sample)
            sample["audio_url"] = self.register_media(Path(sample.get("path", "")), under=path)
            samples.append(sample)
        audio_url = self.register_media(Path(analysis.get("wav_path", "")), under=path) if analysis.get("wav_path") else None
        if not audio_url and summary["source_path"]:
            audio_url = self.register_media(Path(summary["source_path"]))
        warnings = list(analysis.get("warnings", []))
        if summary["old_version"]:
            warnings.insert(0, "这是旧版人物结果。先重新分析人物会另建任务并保留旧文字；开始转写将重新计费。")
        return {**summary, "turns": analysis.get("turns", []), "segments": rows, "samples": samples,
                "speaker_names": names, "warnings": warnings, "audio_url": audio_url,
                "complete": bool(rows) and all(row.get("state") == "done" for row in rows),
                "estimated_cost_yuan": analysis.get("estimated_cost_yuan"),
                "detected_speech_seconds": analysis.get("detected_speech_seconds", 0),
                "ai_summary": self._summary_payload(self._selected, path, snapshot),
                "exports": {key: {"available": (path / filename).is_file()} for key, filename in EXPORT_FILES.items()}}

    def _summary_snapshot(self, path: Path, *, summary=None, rows=None):
        """Use the same corrected text and people shown in the transcript UI."""
        if summary is None:
            summary = self._job_summary(self._selected, path)
        if rows is None:
            analysis, checkpoint, transcript = self._metadata(path)
            names = self._names(path) or transcript.get("speaker_names", {})
            rows = self._overlay(self._rows(analysis, checkpoint, transcript),
                                 self._annotations(path, analysis, strict=True), names)
        segments = [{key: row.get(key, "") for key in
                     ("id", "start", "end", "speaker", "speaker_name", "text", "state", "uncertain")}
                    for row in rows]
        segments.sort(key=lambda row: (row["start"], row["end"], row["id"]))
        count = sum(row["state"] == "done" for row in segments)
        snapshot = {"title": summary["title"], "duration_seconds": summary["duration_seconds"],
                    "segments": segments,
                    "coverage": {"done_count": count, "total_count": len(segments),
                                 "full": bool(segments) and count == len(segments)}}
        snapshot["fingerprint"] = hashlib.sha256(json.dumps(snapshot, ensure_ascii=False,
                                               sort_keys=True).encode("utf-8")).hexdigest()
        return snapshot

    @staticmethod
    def _read_summary(path: Path):
        report = read_json(path / "AI总结.json")
        if not isinstance(report, dict):
            return None
        try:
            content = validate_content(report.get("content"))
            coverage = report["coverage"]
            if (report.get("schema_version") != 1 or not isinstance(report.get("input_fingerprint"), str)
                    or not isinstance(coverage, dict)
                    or not isinstance(coverage.get("full"), bool)
                    or not all(isinstance(coverage.get(key), int) for key in ("done_count", "total_count"))):
                return None
            return {**report, "content": content}
        except (ValueError, TypeError, KeyError, RuntimeError):
            return None

    def _summary_payload(self, key: str, path: Path, snapshot: dict):
        report = self._read_summary(path)
        state = dict(self._ai_summaries.get(key, {}))
        state.setdefault("status", "complete" if report else "empty")
        state.setdefault("message", "总结已保存在本机。" if report else "生成整条录音的总结，并在最后列出待办事项。")
        state["report"] = report
        state["stale"] = bool(report and (report["input_fingerprint"] != snapshot["fingerprint"]
                                          or report.get("prompt_version") != PROMPT_VERSION
                                          or report.get("model") != DEFAULT_MODEL))
        return state

    @staticmethod
    def _write_summary_markdown(path: Path, report: dict, rendered: str | None = None):
        target = path / "AI总结.md"
        temporary = path / ".AI总结.md.tmp"
        temporary.write_text(rendered if rendered is not None else render_markdown(report), encoding="utf-8-sig")
        os.replace(temporary, target)
        return target

    def summarize(self, api_key: str, allow_partial: bool = False):
        if not isinstance(api_key, str) or not api_key.strip() or len(api_key) > 512:
            raise ValueError("请填写有效的 DeepSeek API Key。")
        if not isinstance(allow_partial, bool):
            raise ValueError("仅总结已转写部分必须为开或关。")
        with self._lock:
            self._ensure_idle()
            key = self._selected
            path = self._jobs.get(key or "")
            if path is None:
                raise ValueError("请先选择一个已有转写文字的录音任务。")
            snapshot = self._summary_snapshot(path)
            if not any(row["state"] == "done" and str(row["text"]).strip() for row in snapshot["segments"]):
                raise ValueError("当前任务还没有可总结的文字，请先完成转写。")
            if not snapshot["coverage"]["full"] and not allow_partial:
                raise ValueError("录音尚未全部转写。请继续转写，或明确选择仅总结已转写部分。")
            self._cancel = threading.Event()
            cancel = self._cancel
            self._task = {"running": True, "stage": "summary", "current": 0, "total": 0,
                          "message": "正在准备 AI 总结…", "cancel_requested": False}
            self._ai_summaries[key] = {"status": "running", "message": "正在准备 AI 总结…",
                                       "progress": {"stage": "preparing", "current": 0, "total": 0,
                                                    "message": "正在准备 AI 总结…"}}
            secret = api_key.strip()
            def safe(message):
                return str(message).replace(secret, "[已隐藏]")
            def progress(stage, current, total, message):
                with self._lock:
                    message = safe(message)
                    self._ai_summaries[key]["progress"] = {"stage": safe(stage), "current": current,
                                                            "total": total, "message": message}
                    if not cancel.is_set():
                        self._ai_summaries[key]["message"] = message
                        self._task.update(current=current, total=total, message=message)
            def worker():
                service = None
                job_lock = JobLock(path)
                try:
                    job_lock.__enter__()
                    service = self.summary_factory(secret, log=lambda message: self._log(safe(message)))
                    report = service.generate(snapshot, path, cancel, progress)
                    with self._lock:
                        if cancel.is_set():
                            raise RuntimeError("总结已停止。")
                        # The saved snapshot is authoritative even for injected/test services.
                        report = {**report, "schema_version": 1, "input_fingerprint": snapshot["fingerprint"],
                                  "coverage": snapshot["coverage"], "title": snapshot["title"],
                                  "duration_seconds": snapshot["duration_seconds"],
                                  "content": validate_content(report.get("content"))}
                        rendered = render_markdown(report)
                        atomic_json(path / "AI总结.json", report)
                        try:
                            self._write_summary_markdown(path, report, rendered)
                        except OSError:
                            self._log("AI 总结已保存，Markdown 暂未导出，可稍后点击打开总结重试。")
                        self._ai_summaries[key].update(status="complete", message="AI 总结已保存，待办事项位于最后。")
                        self._task.update(stage="done", message="AI 总结已保存，待办事项位于最后。")
                except Exception as exc:
                    cancelled = cancel.is_set()
                    message = "总结已停止；已完成的分批整理已缓存，原总结保留。" if cancelled else safe(exc)
                    self._log(message)
                    with self._lock:
                        self._ai_summaries[key].update(status="cancelled" if cancelled else "error", message=message)
                        self._task.update(stage="cancelled" if cancelled else "error", message=message)
                finally:
                    service = None
                    job_lock.__exit__()
                    with self._lock:
                        self._task["running"] = False
            self._worker = threading.Thread(target=worker, daemon=True, name="meeting-ai-summary")
            self._worker.start()

    def cancel_summary(self):
        with self._lock:
            if self._task["running"] and self._task["stage"] != "summary":
                raise ValueError("当前运行的是分析或转写任务。")
            self._cancel.set()
            if self._task["running"]:
                message = "正在停止总结；已发出的请求返回后会缓存成功内容。"
                self._task.update(cancel_requested=True, message=message)
                self._ai_summaries[self._selected]["message"] = message

    def open_summary(self):
        with self._lock:
            path = self._jobs.get(self._selected or "")
            report = self._read_summary(path) if path else None
            if not report:
                raise ValueError("当前录音尚未生成 AI 总结。")
            target = self._write_summary_markdown(path, report)
        self.opener(target)

    def state(self):
        with self._lock:
            self._scan_jobs()
            summaries = [self._job_summary(key, path) for key, path in self._jobs.items() if path.is_dir()]
            summaries.sort(key=lambda row: row["updated_at"], reverse=True)
            source = dict(self._source) if self._source else None
            if source:
                source["audio_url"] = self.register_media(Path(source["path"]))
            return {"version": VERSION, "source": source, "settings": dict(self.settings), "jobs": summaries,
                    "selected_job": self._selected_payload(), "task": dict(self._task), "logs": list(self._logs)}

    def _config(self, api_key: str):
        if not self._source:
            raise ValueError("请先选择一份录音。")
        return RunConfig(audio_path=Path(self._source["path"]), output_root=Path(self.settings["output_root"]),
                         api_key=api_key, num_speakers=self.settings["num_speakers"],
                         max_workers=self.settings["max_workers"], hourly_price=self.settings["hourly_price"],
                         retry_uncertain=self.settings["retry_uncertain"])

    def _start(self, analyze: bool, api_key: str = ""):
        with self._lock:
            self._ensure_idle()
            if not analyze and not api_key.strip():
                raise ValueError("请填写 DeepSeek API Key。仅分析人物可以不填。")
            config = self._config(api_key.strip())
            target = job_directory(config)
            config.speaker_names = self._names(target)
            self._selected = self._register_job(target)
            self._cancel = threading.Event()
            self._task = {"running": True, "stage": "preparing", "current": 0, "total": 0,
                          "message": "正在准备人物分析…" if analyze else "正在准备转写…", "cancel_requested": False}
            atomic_json(self.settings_path, dict(self.settings))
            secret = config.api_key
            def safe(message):
                message = str(message)
                return message.replace(secret, "[已隐藏]") if secret else message
            def progress(value):
                with self._lock:
                    for key in ("stage", "current", "total", "message"):
                        if key in value:
                            self._task[key] = safe(value[key]) if key in ("message", "stage") else value[key]
            def worker():
                try:
                    job = self.job_factory(config, log=lambda message: self._log(safe(message)), progress=progress, cancel_event=self._cancel)
                    result = job.analyze_only() if analyze else job.run()
                    with self._lock:
                        self._selected = self._register_job(Path(result["job_dir"]))
                        if not analyze:
                            self._apply_review_exports(Path(result["job_dir"]))
                        self._task.update(stage="analyzed" if analyze else "done" if result.get("complete") else "partial",
                                          message="人物分析完成，可以试听样例并确认姓名。" if analyze else "转写已保存。" if result.get("complete") else "部分结果已保存，请检查未完成片段。")
                except Exception as exc:
                    self._log(safe(exc))
                    with self._lock:
                        self._task.update(stage="cancelled" if self._cancel.is_set() else "error", message="任务已停止，进度已保留。" if self._cancel.is_set() else safe(exc))
                finally:
                    config.api_key = ""
                    with self._lock:
                        self._task["running"] = False
                        self._scan_jobs()
            self._worker = threading.Thread(target=worker, daemon=True, name="meeting-desktop-job")
            self._worker.start()

    def _apply_review_exports(self, path: Path):
        analysis, checkpoint, transcript = self._metadata(path)
        if not checkpoint and not transcript:
            return
        rows = self._rows(analysis, checkpoint, transcript)
        names = self._names(path)
        rows = self._overlay(rows, self._annotations(path, analysis, strict=True), names)
        metadata = checkpoint.get("metadata") or transcript.get("metadata") or {"audio_path": analysis.get("fingerprint", {}).get("path", "会议录音")}
        complete = bool(rows) and all(row.get("state") == "done" for row in rows)
        export_transcript(path, metadata, rows, names, complete=complete)

    def _pick(self, kind: str):
        with self._picker_lock:
            with self._lock:
                self._ensure_idle()
            if self.dialog_picker is None:
                raise ValueError("请在桌面窗口中选择本地文件；浏览器预览不提供系统文件选择。")
            selected = self.dialog_picker(kind)
            if not selected:
                return
            if kind == "audio":
                self.select_audio(selected)
            else:
                with self._lock:
                    self._ensure_idle()
                    self.settings = self._validate_settings({"output_root": str(selected)})
                    atomic_json(self.settings_path, self.settings)
                    self._scan_jobs()

    def dispatch(self, action: str, payload: dict | None = None):
        payload = payload or {}
        if not isinstance(payload, dict):
            raise ValueError("请求内容必须为对象。")
        if action in ("choose_audio", "choose_output"):
            self._pick("audio" if action == "choose_audio" else "output")
        elif action == "summarize":
            if set(payload) - {"api_key", "allow_partial"}:
                raise ValueError("总结请求含有不支持的字段。")
            self.summarize(payload.get("api_key", ""), payload.get("allow_partial", False))
        elif action == "cancel_summary":
            self.cancel_summary()
        elif action == "open_summary":
            self.open_summary()
        elif action == "cancel":
            with self._lock:
                if self._task["running"] and self._task["stage"] == "summary":
                    self.cancel_summary()
                else:
                    self._cancel.set()
                    if self._task["running"]:
                        self._task.update(cancel_requested=True, message="正在停止；已发出的请求返回后会保存进度。")
        elif action == "config":
            with self._lock:
                self._ensure_idle()
                self.settings = self._validate_settings(payload)
                atomic_json(self.settings_path, self.settings)
                self._scan_jobs()
        elif action == "select_job":
            with self._lock:
                self._ensure_idle()
                self._select_job_unlocked(str(payload.get("id", "")))
        elif action in ("analyze", "transcribe"):
            if "retry_uncertain" in payload:
                with self._lock:
                    self._ensure_idle()
                    self.settings = self._validate_settings({"retry_uncertain": payload["retry_uncertain"]})
            self._start(action == "analyze", str(payload.get("api_key", "")))
        elif action in ("speaker_names", "segment_review"):
            with self._lock:
                self._ensure_idle()
                if not self._selected:
                    raise ValueError("请先选择一个已分析的任务。")
                path = self._jobs[self._selected]
                analysis, checkpoint, transcript = self._metadata(path)
                rows = self._rows(analysis, checkpoint, transcript)
                allowed_speakers = {str(row.get("speaker")) for row in rows}
                if action == "speaker_names":
                    names = payload.get("names")
                    if not isinstance(names, dict) or set(names) - allowed_speakers:
                        raise ValueError("人物名称与当前人物编号不匹配。")
                    if any(not isinstance(value, str) or len(value) > 100 for value in names.values()):
                        raise ValueError("每个人物名称请控制在100字以内。")
                    atomic_json(path / "speaker_names.json", {key: value.strip() for key, value in names.items() if value.strip()})
                else:
                    identifier = str(payload.get("id", ""))
                    record = next((row for row in rows if row["id"] == identifier), None)
                    if not record or record.get("state") != "done":
                        raise ValueError("只能校对已成功转写的片段。")
                    changes = {key: value for key, value in payload.items() if key in ("text", "speaker", "reviewed")}
                    if ("text" in changes and (not isinstance(changes["text"], str) or len(changes["text"]) > 100_000)):
                        raise ValueError("校对文字无效或过长。")
                    if "speaker" in changes and changes["speaker"] not in allowed_speakers:
                        raise ValueError("请选择当前录音中已有的人物。")
                    if "reviewed" in changes and not isinstance(changes["reviewed"], bool):
                        raise ValueError("校对状态必须为开或关。")
                    edits = self._annotations(path, analysis, strict=True)
                    edits[identifier] = {**edits.get(identifier, {}), **changes}
                    atomic_json(path / "review_annotations.json", {"schema_version": 1, "fingerprint": analysis.get("fingerprint"), "segments": edits})
                self._apply_review_exports(path)
        elif action == "open_output":
            with self._lock:
                path = self._jobs.get(self._selected or "")
                if path is None:
                    raise ValueError("请先选择一个已有任务。")
                format_name = payload.get("format")
                if format_name is not None:
                    if format_name not in EXPORT_FILES:
                        raise ValueError("导出格式无效。")
                    path = path / EXPORT_FILES[format_name]
                if not path.exists():
                    raise ValueError("结果尚未生成。")
            self.opener(path)
        else:
            raise ValueError("不支持的操作。")
        return self.state()

    def close(self, timeout: float | None = None):
        self.dispatch("cancel")
        worker = self._worker
        if worker and worker.is_alive():
            worker.join(timeout)

