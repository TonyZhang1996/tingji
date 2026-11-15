"""Timeline and subprocess-contract regressions; no network or API key needed."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from meeting_tool.diarization_worker import assess_groups, make_turns, make_windows, merge_regions
from meeting_tool.speaker_backend import _validate_turns


class TimelineTests(unittest.TestCase):
    def test_hours_keep_original_clock_and_same_global_id(self):
        sr = 16000
        regions = [[10 * sr, 12 * sr], [3602 * sr, 3604 * sr],
                   [7205 * sr, 7207 * sr]]
        windows = make_windows(regions)
        turns = make_turns(regions, windows, [7] * len(windows), [False] * len(windows))
        self.assertEqual([item["start"] for item in turns], [10, 3602, 7205])
        self.assertEqual({item["speaker"] for item in turns}, {"S1"})

    def test_long_turn_split_preserves_full_coverage(self):
        sr = 16000
        regions = [[13 * sr, 360 * sr]]
        windows = make_windows(regions)
        turns = make_turns(regions, windows, [0] * len(windows), [False] * len(windows))
        self.assertEqual(turns[0]["start"], 13)
        self.assertEqual(turns[-1]["end"], 360)
        for previous, following in zip(turns, turns[1:]):
            self.assertEqual(previous["end"], following["start"])
        self.assertTrue(all(0 < item["end"] - item["start"] <= 110 for item in turns))

    def test_all_detected_speech_remains_when_person_changes(self):
        regions = [[5000, 255000], [300000, 310000]]
        windows = make_windows(regions)
        labels = [index % 2 for index in range(len(windows))]
        turns = make_turns(regions, windows, labels, [False] * len(windows))
        for start, end in regions:
            covered = sum(max(0, min(end / 16000, turn["end"]) - max(start / 16000, turn["start"]))
                          for turn in turns)
            self.assertAlmostEqual(covered, (end - start) / 16000, places=3)
        _validate_turns(turns)

    def test_nearby_same_person_retains_sentence_context(self):
        regions = [[16000, 48000], [56000, 96000]]
        windows = make_windows(regions)
        turns = make_turns(regions, windows, [0] * len(windows), [False] * len(windows))
        self.assertEqual(len(turns), 1)
        self.assertEqual((turns[0]["start"], turns[0]["end"]), (1, 6))

    def test_overlap_in_external_result_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "时间段"):
            _validate_turns([{"start": 0, "end": 3, "speaker": "S1"},
                             {"start": 2, "end": 4, "speaker": "S2"}])

    def test_block_regions_union_does_not_duplicate_time(self):
        self.assertEqual(merge_regions([[100, 200], [180, 250], [300, 330]]),
                         [[100, 250], [300, 330]])

    def test_uncertain_reply_is_not_diluted_by_long_turn(self):
        regions = [[16000, 960000]]
        windows = make_windows(regions)
        uncertain = [False] * len(windows)
        uncertain[len(windows) // 2] = True
        turns = make_turns(regions, windows, [0] * len(windows), uncertain)
        self.assertEqual(len(turns), 1)
        self.assertTrue(turns[0]["uncertain"])

    def test_small_speaker_is_warned_without_redistributing_time(self):
        turns = [{"start": 0, "end": 100, "speaker": "S1", "uncertain": False},
                 {"start": 101, "end": 102.5, "speaker": "S2", "uncertain": False}]
        stats, warnings = assess_groups(turns, 2)
        self.assertEqual(stats["S1"]["seconds"], 100)
        self.assertEqual(stats["S2"]["seconds"], 1.5)
        self.assertTrue(warnings)
        self.assertEqual([t["speaker"] for t in turns], ["S1", "S2"])
        self.assertFalse(turns[0]["uncertain"])
        self.assertTrue(turns[1]["uncertain"])


if __name__ == "__main__":
    unittest.main()
