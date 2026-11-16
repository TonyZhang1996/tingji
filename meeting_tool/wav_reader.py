"""Bounded reads of the PCM WAV produced by our FFmpeg normalization step."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import wave

SAMPLE_RATE = 16000
MAX_READ_FRAMES = 190 * SAMPLE_RATE
_FORMAT_ERROR = "人物分析需要 16000 Hz、单声道、16 位 PCM WAV，请先转换录音"


@dataclass(frozen=True)
class WavInfo:
    frames: int
    samplerate: int = SAMPLE_RATE
    channels: int = 1

    @property
    def duration(self) -> float:
        return self.frames / self.samplerate


class PCM16Reader:
    """Read explicit blocks as normalized float32; never load a whole meeting."""

    def __init__(self, path: Path):
        try:
            self._wave = wave.open(str(path), "rb")
        except (wave.Error, EOFError):
            raise ValueError(_FORMAT_ERROR) from None
        if (self._wave.getframerate(), self._wave.getnchannels(),
                self._wave.getsampwidth(), self._wave.getcomptype()) != (SAMPLE_RATE, 1, 2, "NONE"):
            self._wave.close()
            raise ValueError(_FORMAT_ERROR)
        self.info = WavInfo(self._wave.getnframes())

    def seek(self, frame: int) -> int:
        if not isinstance(frame, int) or not 0 <= frame <= self.info.frames:
            raise ValueError("音频读取位置超出范围")
        self._wave.setpos(frame)
        return frame

    def read(self, frames: int):
        import numpy as np
        if not isinstance(frames, int) or not 0 <= frames <= MAX_READ_FRAMES:
            raise ValueError("音频必须按不超过 190 秒的片段读取")
        expected = min(frames, self.info.frames - self._wave.tell())
        data = self._wave.readframes(frames)
        if len(data) != expected * 2:
            raise ValueError("WAV 音频内容不完整，请重新转换录音")
        return np.frombuffer(data, dtype="<i2").astype(np.float32) * (1.0 / 32768.0)

    def close(self):
        self._wave.close()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()


def wav_info(path: Path) -> WavInfo:
    with PCM16Reader(path) as reader:
        return reader.info
