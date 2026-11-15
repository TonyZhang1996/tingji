"""Resumable long-meeting workflow: local global diarization -> DeepSeek ASR -> exports."""
from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
import hashlib
import json
import math
import os
from pathlib import Path
import re
import threading
import time

from .audio import CancelledError, normalize_audio, probe_audio, slice_wav, wav_duration
from .deepseek_api import ApiError, DeepSeekASR
from .diarization_worker import VERSION as DIARIZATION_VERSION
from .exports import atomic_json, export_transcript, refresh_exports

WORKFLOW_VERSION = "1.1-global-speakers-v5"


@dataclass
class RunConfig:
    audio_path: Path
    output_root: Path
    api_key: str = field(default="", repr=False)
    num_speakers: int = 0
    speaker_names: dict[str, str] = field(default_factory=dict)
    max_workers: int = 2
    hourly_price: float = 0.39
    retry_uncertain: bool = False


def source_fingerprint(config: RunConfig) -> dict:
    path = Path(config.audio_path).expanduser().resolve()
    if not path.is_file():
        raise ValueError("请选择存在的录音文件。")
    stat = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        digest.update(stream.read(1024 * 1024))
        if stat.st_size > 1024 * 1024:
            stream.seek(max(0, stat.st_size - 1024 * 1024))
            digest.update(stream.read(1024 * 1024))
    return {"path": str(path), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
            "sample_sha256": digest.hexdigest(), "num_speakers": int(config.num_speakers),
            "workflow_version": WORKFLOW_VERSION, "diarization_version": DIARIZATION_VERSION}


def job_directory(config: RunConfig) -> Path:
    fingerprint = source_fingerprint(config)
    job_id = hashlib.sha256(json.dumps(fingerprint, sort_keys=True).encode("utf-8")).hexdigest()[:16]
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", Path(config.audio_path).stem).strip(" .")[:48] or "录音"
    return Path(config.output_root).expanduser().resolve() / f"{stem}_{job_id}"


class JobLock:
    """OS-backed process lock; survives stale lock files without blocking new runs."""
    def __init__(self, job_dir: Path):
        self.path = Path(job_dir) / ".task.lock"
        self.stream = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.path.open("a+b")
        self.stream.seek(0, os.SEEK_END)
        if self.stream.tell() == 0:
            self.stream.write(b"\0")
            self.stream.flush()
        self.stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.stream.close()
            self.stream = None
            raise RuntimeError("这份录音已有任务正在运行。请先停止另一窗口，避免重复提交。") from None
        return self

    def __exit__(self, *_args):
        if self.stream is not None:
            try:
                self.stream.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
            finally:
                self.stream.close()


class _CombinedEvent:
    def __init__(self, *events):
        self.events = events

    def is_set(self):
        return any(event.is_set() for event in self.events)

    def wait(self, timeout=None):
        until = None if timeout is None else time.monotonic() + timeout
        while not self.is_set():
            if until is not None and time.monotonic() >= until:
                return False
            threading.Event().wait(min(0.1, max(0, until - time.monotonic())) if until is not None else 0.1)
        return True


def _normalise_turns(turns: list[dict], duration: float) -> list[dict]:
    result = []
    for raw in sorted(turns, key=lambda turn: float(turn["start"])):
        start = max(0.0, float(raw["start"]))
        end = min(duration, float(raw["end"]))
        if not math.isfinite(start + end) or end <= start:
            continue
        speaker = str(raw.get("speaker", "S0"))
        uncertain = bool(raw.get("uncertain"))
        if result and start < result[-1]["end"]:
            overlap = result[-1]["end"] - start
            if overlap > 0.025:
                raise ValueError("人物分析返回了重叠分段，已停止，以免同一段音频重复提交。")
            start = result[-1]["end"]
        while start < end - 0.00001:
            finish = min(end, start + 110.0)
            result.append({"start": round(start, 6), "end": round(finish, 6), "speaker": speaker,
                           "uncertain": uncertain or finish - start < 0.7})
            start = finish
    return result


class MeetingJob:
    def __init__(self, config: RunConfig, log=None, progress=None, cancel_event: threading.Event | None = None):
        if not 0 <= int(config.num_speakers) <= 20:
            raise ValueError("人物数量请填 0（自动估计）或 1～20。")
        self.config = config
        self.log = log or (lambda _message: None)
        self.progress = progress or (lambda _value: None)
        self.cancel_event = cancel_event or threading.Event()
        self.job_dir = job_directory(config)
        self._fingerprint = source_fingerprint(config)
        self._record_lock = threading.Lock()
        self._network_stop = threading.Event()
        self._records: list[dict] = []
        self._metadata = {}

    def _notify(self, stage: str, current: int = 0, total: int = 0, message: str = ""):
        self.progress({"stage": stage, "current": current, "total": total, "message": message})

    def _check_cancel(self):
        if self.cancel_event.is_set():
            raise CancelledError("已停止，已完成的结果保留，可继续处理。")

    def analyze_only(self) -> dict:
        with JobLock(self.job_dir):
            return self._analyze_unlocked()

    def _has_older_task(self) -> bool:
        """Notice prior versions without opening or changing their paid ledgers."""
        if not self.job_dir.parent.is_dir():
            return False
        identity_keys = ("path", "size", "mtime_ns", "sample_sha256", "num_speakers")
        stem_prefix = self.job_dir.name.rsplit("_", 1)[0] + "_"
        for directory in self.job_dir.parent.iterdir():
            if directory == self.job_dir or not directory.is_dir() or not directory.name.startswith(stem_prefix):
                continue
            try:
                old = json.loads((directory / "analysis.json").read_text(encoding="utf-8"))
                fingerprint = old.get("fingerprint", {})
                if (isinstance(fingerprint, dict)
                        and all(fingerprint.get(key) == self._fingerprint[key] for key in identity_keys)
                        and fingerprint != self._fingerprint):
                    return True
            except (OSError, ValueError, AttributeError):
                continue
        return False

    def _analyze_unlocked(self) -> dict:
        self._check_cancel()
        analysis_path = self.job_dir / "analysis.json"
        cached = None
        if analysis_path.is_file():
            try:
                candidate = json.loads(analysis_path.read_text(encoding="utf-8"))
                if not isinstance(candidate, dict) or candidate.get("fingerprint") != self._fingerprint:
                    raise ValueError("人物缓存不属于当前录音或参数")
                if candidate.get("diarization_version") != DIARIZATION_VERSION:
                    raise ValueError("人物缓存来自其他版本的分人物方法")
                candidate_wav = Path(candidate.get("wav_path", ""))
                if not candidate_wav.is_file() or wav_duration(candidate_wav) <= 0:
                    raise ValueError("人物缓存音频缺失或损坏")
                if not isinstance(candidate.get("turns"), list):
                    raise ValueError("人物缓存分段无效")
                duration = float(candidate["duration_seconds"])
                if not math.isfinite(duration) or abs(wav_duration(candidate_wav) - duration) > 0.01:
                    raise ValueError("人物缓存时长无效")
                candidate["turns"] = _normalise_turns(candidate["turns"], duration)
                for required in ("detected_speech_seconds", "num_speakers_found", "samples"):
                    if required not in candidate:
                        raise ValueError("人物缓存字段缺失")
                cached = candidate
            except (OSError, ValueError, TypeError, KeyError):
                if (self.job_dir / "checkpoint.json").is_file():
                    raise RuntimeError("人物分析缓存无法验证，且已有付费请求记录。为避免重复提交，已停止；请保留该任务目录供检查。") from None
                self.log("人物分析缓存不完整，将重新分析。")
        elif (self.job_dir / "checkpoint.json").is_file():
            raise RuntimeError("人物分析缓存缺失，但已有转写续传记录。为避免重复计费，已停止，请保留任务目录供检查。")
        if cached is not None:
            self.log("已读取此前完成的人物分析，不重复分析。")
            return cached
        if self._has_older_task():
            self.log("已保留这份录音的旧版任务；新版会重新分析人物，并将结果保存在新的任务目录。")
        self._notify("probe", message="正在读取录音时长")
        info = probe_audio(Path(self.config.audio_path), self.cancel_event)
        self.log(f"录音时长 {info['duration_seconds'] / 3600:.2f} 小时，声道 {info['channels']}。")
        self._notify("convert", message="正在转换为适合分析的单声道音频；不会整段载入内存")
        cache_dir = self.job_dir / "cache"
        wav_path = normalize_audio(Path(self.config.audio_path), cache_dir / "recording_16k.wav", self.cancel_event)
        self._check_cancel()
        if source_fingerprint(self.config) != self._fingerprint:
            raise RuntimeError("源录音在处理期间发生变化，请停止编辑该文件后重新开始。")
        actual_duration = wav_duration(wav_path)
        self._notify("diarize", message="正在整场分析说话人，人物编号将贯穿全部录音")
        from .speaker_backend import diarize
        turns = diarize(wav_path, cache_dir / "speakers", num_speakers=self.config.num_speakers,
                        log=self.log, cancel_event=self.cancel_event)
        turns = _normalise_turns(turns, actual_duration)
        self._check_cancel()
        warnings = []
        diagnostics = {}
        try:
            details = json.loads((cache_dir / "speakers" / "diarization.json").read_text(encoding="utf-8"))
            if isinstance(details, dict):
                warnings = [item for item in details.get("warnings", []) if isinstance(item, str)]
                candidate_diagnostics = details.get("clustering", details.get("diagnostics"))
                if isinstance(candidate_diagnostics, dict):
                    diagnostics = candidate_diagnostics
        except (OSError, ValueError, TypeError):
            pass
        samples = []
        sample_dir = self.job_dir / "人物试听片段"
        for speaker in sorted({turn["speaker"] for turn in turns}):
            choices = [turn for turn in turns if turn["speaker"] == speaker]
            selected = max(choices, key=lambda turn: (
                (turn["end"] - turn["start"] >= 3.0) and not turn.get("uncertain"),
                turn["end"] - turn["start"] >= 3.0,
                not turn.get("uncertain"), turn["end"] - turn["start"]))
            start, end = selected["start"], min(selected["end"], selected["start"] + 15)
            too_short = end - start < 3.0
            if too_short:
                label = speaker.replace("S", "说话人 ", 1)
                warnings.append(f"{label} 的可用声音样例只有 {end - start:.1f} 秒，人物归属仍需回听确认。")
            safe_speaker = re.sub(r"[^A-Za-z0-9_-]", "_", speaker)
            sample_path = sample_dir / (safe_speaker + ".wav")
            slice_wav(wav_path, sample_path, start, end, self.cancel_event)
            samples.append({"speaker": speaker, "path": str(sample_path), "start": start, "end": end,
                            "uncertain": bool(selected.get("uncertain", False) or too_short), "too_short": too_short})
        warnings = list(dict.fromkeys(warnings))
        for warning in warnings:
            self.log("人物分析提示：" + warning)
        analysis = {"schema_version": 2, "fingerprint": self._fingerprint, "job_dir": str(self.job_dir),
                    "diarization_version": DIARIZATION_VERSION, "warnings": warnings,
                    "diarization_diagnostics": diagnostics,
                    "wav_path": str(wav_path), "turns": turns, "duration_seconds": actual_duration,
                    "detected_speech_seconds": sum(turn["end"] - turn["start"] for turn in turns),
                    "num_speakers_requested": self.config.num_speakers,
                    "num_speakers_found": len({turn["speaker"] for turn in turns}), "samples": samples,
                    "estimated_billable_seconds": sum(math.ceil(turn["end"] - turn["start"] - 0.000001) for turn in turns),
                    "estimated_cost_yuan": sum(math.ceil(turn["end"] - turn["start"] - 0.000001) for turn in turns) / 3600 * self.config.hourly_price,
                    "source_info": info,
                    "notice": "人物编号由自动声纹聚类得到，身份需试听后确认；短句及重叠讲话可能归属有误。"}
        atomic_json(analysis_path, analysis)
        atomic_json(self.job_dir / "人物分析.json", analysis)
        self.log(f"人物分析完成：{analysis['num_speakers_found']} 人，{len(turns)} 个发言片段。")
        self._notify("analyzed", len(turns), len(turns), "本地人物分析完成；尚未调用 DeepSeek")
        return analysis

    def _checkpoint(self):
        # Called under the record lock whenever a request changes state.
        atomic_json(self.job_dir / "checkpoint.json", {"schema_version": 1, "fingerprint": self._fingerprint,
                    "metadata": self._metadata, "segments": self._records})

    def _initialise_records(self, analysis: dict):
        previous = {}
        checkpoint_path = self.job_dir / "checkpoint.json"
        if checkpoint_path.is_file():
            try:
                checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
                if (not isinstance(checkpoint, dict) or checkpoint.get("fingerprint") != self._fingerprint
                        or not isinstance(checkpoint.get("segments"), list)):
                    raise ValueError("续传记录身份或分段不匹配")
                for row in checkpoint["segments"]:
                    if (not isinstance(row, dict) or not isinstance(row.get("id"), str)
                            or row.get("state") not in ("pending", "requesting", "done", "failed", "ambiguous")
                            or (row.get("state") == "done" and not isinstance(row.get("text"), str))):
                        raise ValueError("续传记录条目无效")
                    if row["id"] in previous:
                        raise ValueError("续传条目重复")
                    previous[row["id"]] = row
            except (OSError, ValueError, TypeError, KeyError):
                # Do not silently submit potentially paid requests if a prior ledger is damaged.
                raise RuntimeError("续传记录无法读取。为避免重复计费，已停止；请先备份并检查 checkpoint.json。") from None
        self._records = []
        for index, turn in enumerate(analysis["turns"]):
            key = hashlib.sha256(json.dumps(turn, sort_keys=True).encode("utf-8")).hexdigest()[:20]
            row = {"id": key, "index": index, **turn, "state": "pending", "text": ""}
            if key in previous:
                old = previous[key]
                row.update({k: v for k, v in old.items() if k not in ("start", "end", "speaker", "uncertain", "index")})
                row["uncertain"] = bool(row.get("uncertain") or old.get("empty_recognition"))
                if row.get("state") == "requesting":
                    row["state"] = "ambiguous"
                    row["error"] = "上次请求在结果写入前中断，服务端可能已计费。"
            self._records.append(row)
        current_ids = {row["id"] for row in self._records}
        if checkpoint_path.is_file() and set(previous) != current_ids:
            raise RuntimeError("已保存的转写片段与当前人物分段不一致。为避免重复计费，已停止，请保留任务目录供检查。")
        self._metadata = {"audio_path": str(Path(self.config.audio_path).resolve()),
                          "duration_seconds": analysis["duration_seconds"],
                          "detected_speech_seconds": analysis["detected_speech_seconds"],
                          "num_speakers_requested": self.config.num_speakers,
                          "num_speakers_found": analysis["num_speakers_found"],
                          "hourly_price_estimate_yuan": self.config.hourly_price,
                          "price_notice": "仅为界面估算；折扣和实际扣费以 DeepSeek 账单为准。",
                          "workflow_version": WORKFLOW_VERSION, "diarization_version": DIARIZATION_VERSION,
                          "diarization_warnings": analysis.get("warnings", [])}
        with self._record_lock:
            self._checkpoint()

    def _transcribe_one(self, index: int):
        record = self._records[index]
        combined = _CombinedEvent(self.cancel_event, self._network_stop)
        if combined.is_set():
            return
        piece = self.job_dir / "cache" / "asr_parts" / (record["id"] + ".wav")
        requested = False
        try:
            slice_wav(self.job_dir / "cache" / "recording_16k.wav", piece, record["start"], record["end"], combined)
            if combined.is_set():
                return
            with self._record_lock:
                record["state"] = "requesting"
                record.pop("error", None)
                record["request_attempts"] = int(record.get("request_attempts", 0)) + 1
                self._checkpoint()
            requested = True
            client = DeepSeekASR(self.config.api_key, log=self.log)
            result = client.transcribe(piece, combined)
            # Save returned text even when the user cancelled while this request was in flight.
            with self._record_lock:
                record.update(result)
                record["state"] = "done"
                if not result["text"].strip():
                    record["uncertain"] = True
                    record["empty_recognition"] = True
                self._checkpoint()
            self.log(f"第 {index + 1}/{len(self._records)} 段已保存。")
        except CancelledError:
            with self._record_lock:
                if record.get("state") == "requesting":
                    # Client cancellation only occurs before a send or during explicit-server-error backoff.
                    record["state"] = "pending"
                self._checkpoint()
        except ApiError as exc:
            self._network_stop.set()
            with self._record_lock:
                record["state"] = "ambiguous" if exc.ambiguous else "failed"
                record["error"] = str(exc)
                record["http_status"] = exc.status
                self._checkpoint()
            self.log(f"第 {index + 1} 段暂停：{exc}")
        except Exception as exc:
            self._network_stop.set()
            # Unknown errors after entering the HTTP call must never trigger a silent resend.
            with self._record_lock:
                record["state"] = "ambiguous" if requested else "failed"
                record["error"] = ("请求结果未确认；可能已计费。" if requested else "分段处理失败。")
                self._checkpoint()
            # Do not render arbitrary exception text from an API transport, which could contain credentials.
            self.log(f"第 {index + 1} 段发生 {type(exc).__name__}，已暂停后续提交。")
        finally:
            piece.unlink(missing_ok=True)

    def run(self) -> dict:
        if not self.config.api_key.strip():
            raise ValueError("请填写 DeepSeek API Key；只分析人物时可以不填。")
        with JobLock(self.job_dir):
            analysis = self._analyze_unlocked()
            self._initialise_records(analysis)
            if self.cancel_event.is_set():
                return self._export(cancelled=True)
            unknown = sum(row["state"] == "ambiguous" for row in self._records)
            if unknown and not self.config.retry_uncertain:
                self.log(f"有 {unknown} 段上次请求结果不明。已保留并跳过；勾选允许重试后才会重新提交。")
            todo = [index for index, row in enumerate(self._records)
                    if row["state"] not in ("done", "ambiguous") or (row["state"] == "ambiguous" and self.config.retry_uncertain)]
            resumed = sum(row["state"] == "done" for row in self._records)
            self.log(f"已有 {resumed} 段成功结果，本次不会重复提交；待处理 {len(todo)} 段。")
            self._notify("transcribe", resumed, len(self._records), "正在分段转写；每段成功后立即保存")
            next_index = 0
            active = {}
            stopping_notified = False
            with ThreadPoolExecutor(max_workers=max(1, min(int(self.config.max_workers), 4)), thread_name_prefix="deepseek-asr") as pool:
                while next_index < len(todo) or active:
                    while (next_index < len(todo) and len(active) < max(1, min(int(self.config.max_workers), 4))
                           and not self.cancel_event.is_set() and not self._network_stop.is_set()):
                        index = todo[next_index]
                        next_index += 1
                        active[pool.submit(self._transcribe_one, index)] = index
                    if not active:
                        break
                    completed, _ = wait(active, timeout=0.25, return_when=FIRST_COMPLETED)
                    for future in completed:
                        active.pop(future)
                        # Unexpected ledger/disk errors are propagated; paid-response states stay 'requesting'.
                        future.result()
                        count = sum(row["state"] == "done" for row in self._records)
                        self._notify("transcribe", count, len(self._records), f"已保存 {count}/{len(self._records)} 段")
                    if self.cancel_event.is_set() and active and not stopping_notified:
                        stopping_notified = True
                        self._notify("stopping", sum(row["state"] == "done" for row in self._records), len(self._records),
                                     "已停止新提交；正在等待已发出的请求并保存结果，避免再次扣费")
            return self._export(cancelled=self.cancel_event.is_set())

    def _export(self, *, cancelled=False) -> dict:
        complete = all(row["state"] == "done" for row in self._records) and not cancelled
        if not self._records:
            # Silence is a valid completed analysis, but clearly state that no speech was found.
            self._metadata["notice"] = "未检测到可转写的语音。"
        self._notify("export", message="正在合并导出完整文字和人物时间片段")
        names_path = self.job_dir / "speaker_names.json"
        names = {}
        if names_path.is_file():
            try:
                names = json.loads(names_path.read_text(encoding="utf-8"))
                if not isinstance(names, dict):
                    raise ValueError("names must be a dictionary")
            except (OSError, ValueError):
                self.log("人物姓名文件无法读取，本次先使用人物编号导出；音频文字结果已保留。")
                names = {}
        # Empty config means 'no override'; an explicit empty file means the user cleared names.
        names.update(self.config.speaker_names or {})
        result = export_transcript(self.job_dir, self._metadata, self._records, names, complete=complete)
        result.update({"cancelled": cancelled, "samples": json.loads((self.job_dir / "analysis.json").read_text(encoding="utf-8")).get("samples", []),
                       "failed_count": sum(row["state"] == "failed" for row in self._records),
                       "ambiguous_count": sum(row["state"] == "ambiguous" for row in self._records)})
        self._notify("done" if complete else "cancelled" if cancelled else "partial",
                     sum(row["state"] == "done" for row in self._records), len(self._records),
                     "转写完成" if complete else "已导出当前结果，尚有片段未完成")
        return result
