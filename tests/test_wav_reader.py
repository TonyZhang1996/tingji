"""PCM reader regressions using small synthetic samples only."""
from __future__ import annotations

from pathlib import Path
import struct
import tempfile
import unittest
import wave

import numpy as np

from meeting_tool.wav_reader import MAX_READ_FRAMES, PCM16Reader, wav_info


class PCMReaderTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "synthetic.wav"

    def write(self, values=(-32768, -16384, 0, 16384, 32767), *, rate=16000, channels=1, width=2):
        with wave.open(str(self.path), "wb") as writer:
            writer.setparams((channels, width, rate, 0, "NONE", "not compressed"))
            writer.writeframes(np.array(values, dtype="<i2").tobytes())

    def test_pcm16_samples_have_expected_float32_values(self):
        self.write()
        with PCM16Reader(self.path) as reader:
            samples = reader.read(5)
            self.assertEqual(samples.dtype, np.float32)
            np.testing.assert_array_equal(samples, np.array([-1., -.5, 0., .5, 32767 / 32768], dtype=np.float32))
        self.assertEqual(wav_info(self.path).frames, 5)
        self.assertEqual(wav_info(self.path).duration, 5 / 16000)

    def test_seek_blocks_tail_and_eof_keep_sample_positions(self):
        self.write()
        with PCM16Reader(self.path) as reader:
            np.testing.assert_array_equal(reader.read(2), [-1., -.5])
            self.assertEqual(reader.seek(3), 3)
            np.testing.assert_array_equal(reader.read(100), [.5, 32767 / 32768])
            self.assertEqual(len(reader.read(100)), 0)
            reader.seek(0)
            self.assertEqual(len(reader.read(0)), 0)
            self.assertEqual(reader.read(1)[0], -1.)

    def test_reads_require_explicit_bounded_block_size(self):
        self.write()
        with PCM16Reader(self.path) as reader:
            for frames in (-1, MAX_READ_FRAMES + 1, None):
                with self.subTest(frames=frames), self.assertRaises(ValueError):
                    reader.read(frames)
            self.assertEqual(len(reader.read(MAX_READ_FRAMES)), 5)

    def test_seek_rejects_out_of_range(self):
        self.write()
        with PCM16Reader(self.path) as reader:
            for frame in (-1, 6, 1.5):
                with self.subTest(frame=frame), self.assertRaises(ValueError):
                    reader.seek(frame)

    def test_rejects_wrong_sample_rate(self):
        self.write(rate=8000)
        with self.assertRaisesRegex(ValueError, "16000"):
            wav_info(self.path)

    def test_rejects_stereo(self):
        self.write(values=(0, 0, 1, 1), channels=2)
        with self.assertRaisesRegex(ValueError, "单声道"):
            wav_info(self.path)

    def test_rejects_non_16_bit_pcm(self):
        for width in (1, 3, 4):
            self.write(values=(0, 0, 0, 0, 0, 0), width=width)
            with self.subTest(width=width), self.assertRaisesRegex(ValueError, "16 位"):
                wav_info(self.path)

    def test_rejects_float_wav(self):
        self.write()
        data = bytearray(self.path.read_bytes())
        struct.pack_into("<H", data, 20, 3)  # WAVE_FORMAT_IEEE_FLOAT
        self.path.write_bytes(data)
        with self.assertRaisesRegex(ValueError, "PCM"):
            wav_info(self.path)

    def test_reports_truncated_sample_data(self):
        self.write()
        self.path.write_bytes(self.path.read_bytes()[:-2])
        with PCM16Reader(self.path) as reader:
            with self.assertRaisesRegex(ValueError, "不完整"):
                reader.read(5)


if __name__ == "__main__":
    unittest.main()
