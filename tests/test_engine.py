from __future__ import annotations

import json
from pathlib import Path
import shutil
import struct
import tempfile
import threading
import unittest
from unittest.mock import patch
import wave

from meeting_tool.audio import slice_wav, wav_duration
from meeting_tool.deepseek_api import ApiError
from meeting_tool import engine as engine_module
from meeting_tool.engine import JobLock, MeetingJob, RunConfig, _normalise_turns, job_directory
from meeting_tool.exports import atomic_json, refresh_exports


def make_wav(path: Path, seconds: float = 4):
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(16000)
        out.writeframes(struct.pack("<h", 100) * int(seconds * 16000))


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "会议.wav"
        make_wav(self.source)
        self.config = RunConfig(self.source, self.root / "out", "mock-key-do-not-send", num_speakers=2, max_workers=1)

    def tearDown(self):
        self.temp.cleanup()

    def prepared_job(self, config=None, event=None):
        job = MeetingJob(config or self.config, cancel_event=event)
        wav_path = job.job_dir / "cache" / "recording_16k.wav"
        wav_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self.source, wav_path)
        turns = [{"start": 0.0, "end": 1.1, "speaker": "S1", "uncertain": False},
                 {"start": 1.2, "end": 2.1, "speaker": "S2", "uncertain": False},
                 {"start": 2.2, "end": 3.8, "speaker": "S1", "uncertain": False}]
        atomic_json(job.job_dir / "analysis.json", {
            "fingerprint": job._fingerprint, "job_dir": str(job.job_dir), "wav_path": str(wav_path),
            "diarization_version": engine_module.DIARIZATION_VERSION,
            "turns": turns, "duration_seconds": 4.0, "detected_speech_seconds": 3.6,
            "num_speakers_found": 2, "samples": [],
        })
        return job

    def test_multihour_split_preserves_timeline_and_api_limit(self):
        turns = _normalise_turns([{"start": 0, "end": 4 * 3600 + 1, "speaker": "S1"}], 4 * 3600 + 1)
        self.assertEqual(turns[-1]["end"], 14401)
        self.assertTrue(all(0 < row["end"] - row["start"] <= 110 for row in turns))
        self.assertTrue(all(left["end"] == right["start"] for left, right in zip(turns, turns[1:])))

    def test_overlap_rejected_before_charging(self):
        with self.assertRaises(ValueError):
            _normalise_turns([{"start": 0, "end": 3}, {"start": 2, "end": 4}], 4)

    def test_os_lock_blocks_same_job(self):
        directory = job_directory(self.config)
        with JobLock(directory):
            with self.assertRaises(RuntimeError):
                with JobLock(directory):
                    self.fail("second process lock should not succeed")
        with JobLock(directory):
            pass

    def test_parameter_change_uses_separate_ledger(self):
        other = RunConfig(self.source, self.config.output_root, num_speakers=3)
        self.assertNotEqual(job_directory(self.config), job_directory(other))

    def test_new_speaker_version_uses_new_task_and_preserves_paid_legacy_result(self):
        with patch.object(engine_module, "WORKFLOW_VERSION", "1.0-global-turns-110s"), \
                patch.object(engine_module, "DIARIZATION_VERSION", "speaker-v4-stream-global"):
            legacy = self.prepared_job()
            legacy_paid = {"fingerprint": legacy._fingerprint,
                           "segments": [{"id": "old-paid", "state": "done", "text": "旧版已付费文字"}]}
            atomic_json(legacy.job_dir / "checkpoint.json", legacy_paid)
        before = {path.relative_to(legacy.job_dir): path.read_bytes()
                  for path in legacy.job_dir.rglob("*") if path.is_file()}
        log = []
        upgraded = MeetingJob(self.config, log=log.append)
        self.assertNotEqual(upgraded.job_dir, legacy.job_dir)

        def fake_diarize(_wav, _dir, **kwargs):
            return [{"start": 0.0, "end": 1.5, "speaker": "S1", "uncertain": False},
                    {"start": 1.8, "end": 3.8, "speaker": "S2", "uncertain": False}]

        with patch("meeting_tool.speaker_backend.diarize", side_effect=fake_diarize) as diarize, \
                patch("meeting_tool.engine.DeepSeekASR") as asr:
            result = upgraded.analyze_only()
        self.assertEqual(diarize.call_count, 1)
        asr.assert_not_called()
        self.assertEqual(result["diarization_version"], engine_module.DIARIZATION_VERSION)
        self.assertFalse((upgraded.job_dir / "checkpoint.json").exists())
        self.assertTrue(any("旧版" in item and "新" in item for item in log))
        after = {path.relative_to(legacy.job_dir): path.read_bytes()
                 for path in legacy.job_dir.rglob("*") if path.is_file()}
        self.assertEqual(after, before)

    def test_wrong_algorithm_cache_with_paid_ledger_stops_without_resubmission(self):
        job = self.prepared_job()
        analysis_path = job.job_dir / "analysis.json"
        analysis = json.loads(analysis_path.read_text(encoding="utf-8"))
        analysis["diarization_version"] = "older-method"
        atomic_json(analysis_path, analysis)
        atomic_json(job.job_dir / "checkpoint.json", {"fingerprint": job._fingerprint, "segments": []})
        before = (job.job_dir / "checkpoint.json").read_bytes()
        with patch("meeting_tool.engine.DeepSeekASR") as asr, \
                patch("meeting_tool.speaker_backend.diarize") as diarize:
            with self.assertRaisesRegex(RuntimeError, "已有付费请求记录"):
                job.run()
        asr.assert_not_called()
        diarize.assert_not_called()
        self.assertEqual((job.job_dir / "checkpoint.json").read_bytes(), before)

    def test_analysis_prefers_long_sample_and_marks_short_sample_unreliable(self):
        make_wav(self.source, 24)
        config = RunConfig(self.source, self.config.output_root, num_speakers=3)
        job = MeetingJob(config)

        def fake_diarize(_wav, work_dir, **kwargs):
            Path(work_dir).mkdir(parents=True)
            atomic_json(Path(work_dir) / "diarization.json", {
                "warnings": ["自动人物分组需要回听核对。"],
                "clustering": {"window_counts": [10, 8, 1]},
            })
            return [{"start": 0.0, "end": 1.0, "speaker": "S1", "uncertain": False},
                    {"start": 2.0, "end": 8.0, "speaker": "S1", "uncertain": True},
                    {"start": 9.0, "end": 17.0, "speaker": "S2", "uncertain": False},
                    {"start": 18.0, "end": 20.0, "speaker": "S3", "uncertain": False}]

        with patch("meeting_tool.speaker_backend.diarize", side_effect=fake_diarize), \
                patch("meeting_tool.engine.DeepSeekASR") as asr:
            result = job.analyze_only()
        samples = {row["speaker"]: row for row in result["samples"]}
        self.assertEqual(samples["S1"]["start"], 2.0)
        self.assertEqual(samples["S1"]["end"], 8.0)
        self.assertTrue(samples["S1"]["uncertain"])
        self.assertFalse(samples["S2"]["too_short"])
        self.assertTrue(samples["S3"]["too_short"])
        self.assertTrue(samples["S3"]["uncertain"])
        self.assertTrue(any("说话人 3" in item and "2.0 秒" in item for item in result["warnings"]))
        self.assertIn("自动人物分组需要回听核对。", result["warnings"])
        self.assertEqual(result["diarization_diagnostics"]["window_counts"], [10, 8, 1])
        asr.assert_not_called()

    def test_completed_chunks_are_never_resubmitted_and_names_reexport(self):
        calls = []
        class FakeASR:
            def __init__(self, *_args, **_kwargs): pass
            def transcribe(self, path, event):
                calls.append(path.name)
                return {"text": "原话" + str(len(calls)), "request_id": "mock", "duration_ms": 1000}
        job = self.prepared_job()
        with patch("meeting_tool.engine.DeepSeekASR", FakeASR):
            result = job.run()
            self.assertTrue(result["complete"])
            self.assertEqual(len(calls), 3)
            resumed = self.prepared_job().run()
            self.assertTrue(resumed["complete"])
            self.assertEqual(len(calls), 3)
        renamed = refresh_exports(job.job_dir, {"S1": "甲", "S2": "乙"})
        text = Path(renamed["txt_path"]).read_text(encoding="utf-8-sig")
        self.assertIn("甲", text)
        self.assertIn("乙", text)
        self.assertNotIn("mock-key-do-not-send", "\n".join(file.read_text(encoding="utf-8", errors="ignore")
                         for file in job.job_dir.glob("*.json")))

    def test_ambiguous_stops_new_submissions_and_requires_explicit_retry(self):
        calls = []
        class AmbiguousASR:
            def __init__(self, *_args, **_kwargs): pass
            def transcribe(self, path, event):
                calls.append(path.name)
                raise ApiError("mock timeout", ambiguous=True)
        with patch("meeting_tool.engine.DeepSeekASR", AmbiguousASR):
            first = self.prepared_job().run()
        self.assertFalse(first["complete"])
        self.assertEqual(first["ambiguous_count"], 1)
        self.assertEqual(len(calls), 1)
        class SuccessASR:
            def __init__(self, *_args, **_kwargs): pass
            def transcribe(self, path, event):
                calls.append(path.name)
                return {"text": "已保存", "request_id": "mock", "duration_ms": 1000}
        with patch("meeting_tool.engine.DeepSeekASR", SuccessASR):
            second = self.prepared_job().run()
            self.assertFalse(second["complete"])
            self.assertEqual(len(calls), 3)  # two new segments, ambiguous skipped
            self.config.retry_uncertain = True
            third = self.prepared_job().run()
            self.assertTrue(third["complete"])
            self.assertEqual(len(calls), 4)

    def test_401_stops_later_chunks(self):
        calls = []
        class AuthErrorASR:
            def __init__(self, *_args, **_kwargs): pass
            def transcribe(self, path, event):
                calls.append(path.name)
                raise ApiError("invalid key", status=401)
        with patch("meeting_tool.engine.DeepSeekASR", AuthErrorASR):
            result = self.prepared_job().run()
        self.assertFalse(result["complete"])
        self.assertEqual(result["failed_count"], 1)
        self.assertEqual(len(calls), 1)

    def test_cancel_during_request_saves_success_and_next_run_resumes(self):
        event = threading.Event()
        calls = []
        class CancelDuringASR:
            def __init__(self, *_args, **_kwargs): pass
            def transcribe(self, path, _event):
                calls.append(path.name)
                event.set()
                return {"text": "已返回仍保存", "request_id": "mock", "duration_ms": 1000}
        with patch("meeting_tool.engine.DeepSeekASR", CancelDuringASR):
            result = self.prepared_job(event=event).run()
        self.assertTrue(result["cancelled"])
        self.assertFalse(result["complete"])
        self.assertEqual(result["segments"][0]["state"], "done")
        self.assertEqual(len(calls), 1)

    def test_interrupted_request_is_ambiguous_after_restart(self):
        job = self.prepared_job()
        job._initialise_records(job._analyze_unlocked())
        job._records[0]["state"] = "requesting"
        job._checkpoint()
        recovered = self.prepared_job()
        recovered._initialise_records(recovered._analyze_unlocked())
        self.assertEqual(recovered._records[0]["state"], "ambiguous")

    def test_malformed_or_wrong_fingerprint_checkpoint_cannot_trigger_resubmission(self):
        for malformed in ({"segments": []}, {"fingerprint": {}, "segments": []},
                          {"fingerprint": "wrong", "segments": "not-a-list"}):
            job = self.prepared_job()
            atomic_json(job.job_dir / "checkpoint.json", malformed)
            with self.assertRaisesRegex(RuntimeError, "避免重复计费"):
                job._initialise_records(job._analyze_unlocked())

    def test_saved_person_names_survive_run_with_empty_config_names(self):
        job = self.prepared_job()
        atomic_json(job.job_dir / "speaker_names.json", {"S1": "已确认甲", "S2": "已确认乙"})
        class SuccessASR:
            def __init__(self, *_args, **_kwargs): pass
            def transcribe(self, path, event):
                return {"text": "原话", "request_id": "mock", "duration_ms": 1000}
        with patch("meeting_tool.engine.DeepSeekASR", SuccessASR):
            result = job.run()
        self.assertEqual(result["segments"][0]["speaker_name"], "已确认甲")
        atomic_json(job.job_dir / "speaker_names.json", {})
        with patch("meeting_tool.engine.DeepSeekASR", SuccessASR):
            result = self.prepared_job().run()
        self.assertEqual(result["segments"][0]["speaker_name"], "说话人 1")

    def test_missing_rows_in_valid_checkpoint_are_not_silently_resubmitted(self):
        job = self.prepared_job()
        atomic_json(job.job_dir / "checkpoint.json", {"fingerprint": job._fingerprint, "segments": []})
        with self.assertRaisesRegex(RuntimeError, "避免重复计费"):
            job._initialise_records(job._analyze_unlocked())

    def test_empty_recognition_stays_uncertain_on_resume(self):
        class EmptyASR:
            def __init__(self, *_args, **_kwargs): pass
            def transcribe(self, path, event):
                return {"text": "", "request_id": "mock", "duration_ms": 1000}
        job = self.prepared_job()
        with patch("meeting_tool.engine.DeepSeekASR", EmptyASR):
            first = job.run()
            second = self.prepared_job().run()
        self.assertTrue(all(row["uncertain"] for row in first["segments"]))
        self.assertTrue(all(row["uncertain"] for row in second["segments"]))

    def test_slice_exact_frames_without_repeating_boundary(self):
        first, second = self.root / "a.wav", self.root / "b.wav"
        duration_a = slice_wav(self.source, first, 0.0, 1.2345)
        duration_b = slice_wav(self.source, second, 1.2345, 4.0)
        self.assertAlmostEqual(duration_a + duration_b, 4)
        self.assertAlmostEqual(wav_duration(first) + wav_duration(second), 4)


if __name__ == "__main__":
    unittest.main()
