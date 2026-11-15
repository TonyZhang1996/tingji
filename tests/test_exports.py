from pathlib import Path
import tempfile
import unittest

from meeting_tool.exports import export_transcript, timestamp


class ExportTests(unittest.TestCase):
    def test_long_timestamps_do_not_wrap_at_24h(self):
        self.assertEqual(timestamp(25 * 3600 + 0.123), "25:00:00.123")
        self.assertEqual(timestamp(3.9996, srt=True), "00:00:04,000")

    def test_partial_and_missing_text_are_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            result = export_transcript(Path(directory), {"audio_path": "会议.wav"}, [
                {"start": 0, "end": 1, "speaker": "S1", "state": "done", "text": "原话"},
                {"start": 1, "end": 2, "speaker": "S2", "state": "ambiguous", "text": ""},
            ], {"S1": "甲"}, complete=False)
            text = Path(result["txt_path"]).read_text(encoding="utf-8-sig")
            self.assertIn("尚未完成", text)
            self.assertIn("本段结果不明", text)
            self.assertIn("甲", text)
            self.assertIn("并非逐字对齐", text)
            self.assertFalse(result["complete"])


if __name__ == "__main__":
    unittest.main()
