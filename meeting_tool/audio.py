"""Bounded-memory audio conversion and slicing. No network access."""
from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
import wave

from .runtime_paths import is_frozen, resource_path
from .exports import atomic_json

AUDIO_PREPROCESS_VERSION = "voice-clean-v1"


class CancelledError(RuntimeError):
    pass


def _check_cancel(cancel_event: threading.Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise CancelledError("已停止；已完成的结果会保留。")


def find_binary(name: str) -> str:
    if is_frozen():
        bundled = resource_path("bin", name + (".exe" if os.name == "nt" else ""))
        if bundled.is_file():
            return str(bundled)
        raise RuntimeError(f"安装目录内缺少 {name}，请重新安装听记。")
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


def _enhance_signature(path: Path) -> dict:
    """Validate the PCM payload and fingerprint it without loading a long recording."""
    with wave.open(str(path), "rb") as stream:
        layout = [stream.getnchannels(), stream.getsampwidth(), stream.getframerate(), stream.getnframes()]
        if layout[:3] != [1, 2, 16000] or layout[3] <= 0 or stream.getcomptype() != "NONE":
            raise ValueError("人声增强需要有效的16kHz、16位单声道WAV。")
        stream.setpos(layout[3] - 1)
        if len(stream.readframes(1)) != 2:
            raise ValueError("增强音频文件不完整，请重新处理。")
    stat = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        digest.update(stream.read(65536))
        if stat.st_size > 65536:
            stream.seek(max(0, stat.st_size - 65536))
            digest.update(stream.read(65536))
    return {"path": str(path.resolve()), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
            "edge_sha256": digest.hexdigest(), "layout": layout}


def enhance_audio(source: Path, target: Path, cancel_event: threading.Event | None = None,
                  progress=None) -> Path:
    """Optional bounded-memory spectral cleaning; no new FFmpeg filters/models.

    A sqrt-Hann overlap-add uses original sample coordinates throughout. Padding
    exists only inside the transform, and is never written into the recording.
    The enhanced result is used for ASR only, never for speaker embeddings.
    """
    source, target = Path(source).resolve(), Path(target).resolve()
    if source == target:
        raise ValueError("增强音频必须另存，不能覆盖原录音。")
    _check_cancel(cancel_event)
    original = _enhance_signature(source)
    metadata_path = target.with_name(target.name + ".enhance.json")
    if target.is_file() and metadata_path.is_file():
        try:
            cached = json.loads(metadata_path.read_text(encoding="utf-8"))
            if (isinstance(cached, dict) and cached.get("version") == AUDIO_PREPROCESS_VERSION and cached.get("source") == original
                    and cached.get("output") == _enhance_signature(target)
                    and cached["output"]["layout"] == original["layout"]):
                if progress:
                    progress(original["layout"][3], original["layout"][3])
                return target
        except (OSError, ValueError, KeyError, wave.Error, EOFError):
            pass
    try:
        import numpy as np
    except ImportError:
        raise RuntimeError("人声增强组件不可用，请安装完整版本，或关闭人声增强后重试。") from None
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".enhancing.wav")
    frame_size, hop = 1024, 256
    total = original["layout"][3]
    window = np.sqrt(np.hanning(frame_size + 1)[:-1])
    window_square = window * window
    profile_count = min(2048, max(1, total // frame_size))
    spectra = np.empty((profile_count, frame_size // 2 + 1), dtype=np.float32)
    energies = np.empty(profile_count, dtype=np.float64)
    try:
        # A bounded sample of quieter frames estimates background without keeping
        # hours of audio in memory or assuming the recording starts with silence.
        with wave.open(str(source), "rb") as reader:
            for index, start in enumerate(np.linspace(0, max(0, total - frame_size), profile_count, dtype=np.int64)):
                _check_cancel(cancel_event)
                reader.setpos(int(start))
                raw = np.frombuffer(reader.readframes(frame_size), dtype="<i2").astype(np.float64) / 32768.0
                frame = np.zeros(frame_size)
                frame[:len(raw)] = raw
                energies[index] = np.mean(frame * frame)
                spectra[index] = np.abs(np.fft.rfft(frame * window)) ** 2
        quiet = np.argsort(energies)[:max(1, profile_count // 5)]
        profile = np.median(spectra[quiet], axis=0).astype(np.float64)
        # Frequency median avoids treating a persistent narrow voice harmonic as
        # background. Suppression is capped at 8dB even for a poor estimate.
        profile = np.median(np.lib.stride_tricks.sliding_window_view(np.pad(profile, 4, mode="edge"), 9), axis=-1)
        profile_power = float(profile.sum())
        minimum_gain = 10 ** (-8 / 20)
        frequencies = np.fft.rfftfreq(frame_size, 1 / 16000)
        highpass = np.minimum(1.0, frequencies / 70.0)
        previous_mask = np.ones_like(profile)
        history = np.zeros(frame_size)
        overlap = np.zeros(frame_size)
        weights = np.zeros(frame_size)
        gain = 1.0
        written = 0
        steps = (total + hop - 1) // hop + frame_size // hop - 1
        with wave.open(str(source), "rb") as reader, wave.open(str(temporary), "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(16000)
            for step in range(steps):
                if step % 64 == 0:
                    _check_cancel(cancel_event)
                raw = reader.readframes(hop)
                samples = np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32768.0
                history[:-hop] = history[hop:]
                history[-hop:] = 0
                history[-hop:][:len(samples)] = samples
                spectrum = np.fft.rfft(history * window)
                power = np.abs(spectrum) ** 2
                mask = np.sqrt(np.maximum(1.0 - 0.8 * profile / (power + 1e-20), minimum_gain ** 2))
                previous_mask = 0.65 * previous_mask + 0.35 * mask
                overlap += np.fft.irfft(spectrum * previous_mask * highpass, frame_size) * window
                weights += window_square
                start = (step + 1) * hop - frame_size
                left, right = max(0, -start), min(hop, total - start)
                if right > left:
                    block = overlap[left:right] / np.maximum(weights[left:right], 1e-12)
                    rms = float(np.sqrt(np.mean(block * block)))
                    speech_like = float(power.sum()) > 3 * profile_power and rms > 0.002
                    desired_gain = 1.35 if speech_like else 1.0
                    safe_gain = 0.97 / max(float(np.max(np.abs(block))), 1e-12)
                    gain = min(safe_gain, 0.90 * gain + 0.10 * desired_gain)
                    pcm = np.rint(block * gain * 32767.0).astype("<i2")
                    writer.writeframesraw(pcm.tobytes())
                    written += len(pcm)
                overlap[:-hop] = overlap[hop:]
                overlap[-hop:] = 0
                weights[:-hop] = weights[hop:]
                weights[-hop:] = 0
                if progress and step % 64 == 0:
                    progress(min(written, total), total)
        _check_cancel(cancel_event)
        if written != total or _enhance_signature(temporary)["layout"] != original["layout"]:
            raise RuntimeError("人声增强前后采样帧数不一致，已停止，避免时间轴偏移。")
        if _enhance_signature(source) != original:
            raise RuntimeError("原音频在增强期间发生变化，已停止，请重新开始。")
        os.replace(temporary, target)
        atomic_json(metadata_path, {"version": AUDIO_PREPROCESS_VERSION, "method": "numpy-stft-8db-peak-safe",
                                   "source": original, "output": _enhance_signature(target)})
        if progress:
            progress(total, total)
        return target
    except CancelledError:
        raise
    except Exception as exc:
        raise RuntimeError("本地人声增强失败，未发送新的转写请求。请关闭增强重试，或检查磁盘与音频组件。") from exc
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
