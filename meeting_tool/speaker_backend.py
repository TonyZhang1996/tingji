"""Run the installed, offline speaker models in their existing Python environment."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import queue
import subprocess
import threading
from typing import Callable

DEFAULT_PYTHON = Path(r"D:\miniconda3\envs\firered-speaker\python.exe")
DEFAULT_SPEAKER_ROOT = Path(r"D:\Project\3D-Speaker")


def environment_status() -> tuple[bool, str]:
    """A fast local check; it neither imports the models nor downloads anything."""
    python = Path(os.environ.get("MEETING_SPEAKER_PYTHON", str(DEFAULT_PYTHON)))
    root = Path(os.environ.get("MEETING_SPEAKER_ROOT", str(DEFAULT_SPEAKER_ROOT)))
    model = root / "pretrained_models/campplus/campplus_cn_en_common.pt"
    if not python.is_file():
        return False, f"未找到本地人物分析环境：{python}"
    if not model.is_file():
        return False, f"未找到本地声纹模型：{model}"
    return True, "已找到本地人物分析环境和声纹模型，无需下载模型"


def _validate_turns(turns: object) -> list[dict]:
    if not isinstance(turns, list) or not turns:
        raise RuntimeError("人物分析结果格式错误，请重新分析")
    previous_end = 0.0
    checked = []
    for turn in turns:
        if not isinstance(turn, dict):
            raise RuntimeError("人物分析结果格式错误")
        start, end = float(turn["start"]), float(turn["end"])
        speaker = str(turn["speaker"])
        if (not math.isfinite(start) or not math.isfinite(end)
                or start < previous_end - 0.002 or start < 0 or end <= start
                or end - start > 110.01
                or not speaker.startswith("S") or not speaker[1:].isdigit()):
            raise RuntimeError("人物分析时间段不正确，请重新分析")
        checked.append({"start": start, "end": end, "speaker": speaker,
                        "uncertain": bool(turn.get("uncertain", True))})
        previous_end = end
    return checked


def diarize(wav_path: Path, work_dir: Path, num_speakers: int = 0,
            log: Callable[[str], None] = lambda message: None,
            cancel_event: threading.Event | None = None) -> list[dict]:
    """Return globally numbered speaker turns, using only local installed models.

    Set MEETING_SPEAKER_PYTHON / MEETING_SPEAKER_ROOT to reuse another compatible
    existing installation. Stopping preserves phase checkpoints for the next run.
    """
    if not 0 <= int(num_speakers) <= 20:
        raise ValueError("会议人数应为 0（自动估计）或 1～20")
    wav_path, work_dir = Path(wav_path).resolve(), Path(work_dir).resolve()
    if not wav_path.is_file():
        raise FileNotFoundError(wav_path)
    available, status = environment_status()
    if not available:
        raise RuntimeError(status)
    work_dir.mkdir(parents=True, exist_ok=True)
    output = work_dir / "diarization.json"
    python = Path(os.environ.get("MEETING_SPEAKER_PYTHON", str(DEFAULT_PYTHON)))
    command = [str(python), "-X", "utf8", "-u", str(Path(__file__).with_name("diarization_worker.py")),
               "--wav", str(wav_path), "--output", str(output),
               "--speakers", str(int(num_speakers)), "--speaker-root",
               os.environ.get("MEETING_SPEAKER_ROOT", str(DEFAULT_SPEAKER_ROOT))]
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["OMP_NUM_THREADS"] = "4"
    env["HF_HUB_OFFLINE"] = "1"
    env["TRANSFORMERS_OFFLINE"] = "1"
    messages: queue.Queue[str | None] = queue.Queue()
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, encoding="utf-8", errors="replace", env=env,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def read_output():
        try:
            if process.stdout:
                for line in process.stdout:
                    messages.put(line.rstrip())
        finally:
            messages.put(None)

    reader = threading.Thread(target=read_output, daemon=True)
    reader.start()
    last_error = None
    finished_output = False
    try:
        while not finished_output or process.poll() is None:
            if cancel_event is not None and cancel_event.is_set():
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
                raise RuntimeError("已暂停人物分析，已保存的进度可在下次继续")
            try:
                line = messages.get(timeout=0.1)
            except queue.Empty:
                continue
            if line is None:
                finished_output = True
                continue
            try:
                message = json.loads(line)
            except (ValueError, TypeError):
                # Third-party libraries may print warnings. They contain no key.
                if line:
                    log(line)
                continue
            if not isinstance(message, dict):
                continue
            if message.get("event") == "error":
                last_error = str(message.get("message", "人物分析失败"))
            elif message.get("event") == "progress":
                log(str(message.get("message", "")))
        code = process.wait()
        if code:
            raise RuntimeError(last_error or f"本地人物分析未完成（退出码 {code}），可以重试")
        payload = json.loads(output.read_text(encoding="utf-8"))
        return _validate_turns(payload.get("turns"))
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        if process.stdout:
            process.stdout.close()
        reader.join(timeout=1)
