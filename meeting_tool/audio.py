"""Bounded-memory audio conversion and slicing. No network access."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
import wave


class CancelledError(RuntimeError):
    pass


def _check_cancel(cancel_event: threading.Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise CancelledError("已停止；已完成的结果会保留。")


def find_binary(name: str) -> str:
    found = shutil.which(name)
    if found:
        return found
    local = Path(__file__).resolve().parent.parent / "bin" / (name + (".exe" if os.name == "nt" else ""))
    if local.is_file():
        return str(local)
    raise RuntimeError(f"未找到 {name}。请将 FFmpeg 的 bin 目录加入 PATH，或把程序放入工具的 bin 文件夹。")


def _run(command: list[str], cancel_event: threading.Event | None = None, timeout: float | None = None) -> bytes:
    _check_cancel(cancel_event)
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=flags)
    started = time.monotonic()
    try:
        while True:
            _check_cancel(cancel_event)
            if timeout is not None and time.monotonic() - started > timeout:
                raise RuntimeError("音频程序处理超时，请检查文件是否可正常播放。")
            try:
                stdout, stderr = process.communicate(timeout=0.25)
                break
            except subprocess.TimeoutExpired:
                continue
        if process.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()[-1800:]
            raise RuntimeError("音频处理失败：" + (detail or f"退出码 {process.returncode}"))
        return stdout
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()


def probe_audio(path: Path, cancel_event: threading.Event | None = None) -> dict:
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise ValueError("录音文件不存在。")
    data = json.loads(_run([
        find_binary("ffprobe"), "-v", "error", "-select_streams", "a:0",
        "-show_entries", "format=duration:stream=codec_name,sample_rate,channels,duration",
        "-of", "json", str(path),
    ], cancel_event, timeout=60))
    streams = data.get("streams") or []
    if not streams:
        raise ValueError("文件中没有可读取的音轨。")
    stream = streams[0]
    raw_duration = (data.get("format") or {}).get("duration") or stream.get("duration")
    try:
        duration = float(raw_duration)
    except (TypeError, ValueError):
        raise ValueError("无法读取音频时长，请先确认文件可正常播放。") from None
    if duration <= 0:
        raise ValueError("录音没有有效时长。")
    return {"path": str(path), "duration_seconds": duration, "channels": int(stream.get("channels", 0)),
            "sample_rate": int(stream.get("sample_rate", 0)), "codec": stream.get("codec_name", "unknown"),
            "file_bytes": path.stat().st_size}


def wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as reader:
        return reader.getnframes() / reader.getframerate()


def normalize_audio(source: Path, target: Path, cancel_event: threading.Event | None = None) -> Path:
    """Convert the first audio track to 16kHz/16-bit/mono; mixed from all channels."""
    source, target = Path(source), Path(target)
    _check_cancel(cancel_event)
    if target.is_file():
        try:
            with wave.open(str(target), "rb") as reader:
                if (reader.getnchannels(), reader.getsampwidth(), reader.getframerate()) == (1, 2, 16000) and reader.getnframes() > 0:
                    return target
        except (wave.Error, EOFError, OSError):
            pass
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".converting.wav")
    try:
        _run([find_binary("ffmpeg"), "-nostdin", "-v", "error", "-y", "-i", str(source),
              "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", "-f", "wav", str(temporary)], cancel_event)
        _check_cancel(cancel_event)
        os.replace(temporary, target)
        return target
    finally:
        temporary.unlink(missing_ok=True)


def slice_wav(source: Path, target: Path, start: float, end: float,
              cancel_event: threading.Event | None = None) -> float:
    """Read only the requested interval, in bounded chunks, preserving sample boundaries."""
    if not (0 <= start < end):
        raise ValueError("分段起止时间无效。")
    _check_cancel(cancel_event)
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".part")
    try:
        with wave.open(str(source), "rb") as reader:
            rate = reader.getframerate()
            first = min(round(start * rate), reader.getnframes())
            last = min(round(end * rate), reader.getnframes())
            if first >= last:
                raise ValueError("音频分段没有有效采样。")
            reader.setpos(first)
            remaining = last - first
            with wave.open(str(temporary), "wb") as writer:
                writer.setparams(reader.getparams())
                writer.setnframes(remaining)
                while remaining:
                    _check_cancel(cancel_event)
                    count = min(remaining, rate * 2)
                    raw = reader.readframes(count)
                    if not raw:
                        raise RuntimeError("读取分段时音频提前结束。")
                    writer.writeframesraw(raw)
                    remaining -= len(raw) // (reader.getnchannels() * reader.getsampwidth())
        os.replace(temporary, target)
        return (last - first) / rate
    finally:
        temporary.unlink(missing_ok=True)
