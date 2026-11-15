"""Synthetic voice-feature regressions; no user audio, models, network, or keys."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from meeting_tool.diarization_worker import cluster

HAVE_FEATURE_LIBS = bool(importlib.util.find_spec("numpy") and importlib.util.find_spec("sklearn"))


@unittest.skipUnless(HAVE_FEATURE_LIBS, "Run feature regressions with the existing speaker Python environment")
class ClusteringTests(unittest.TestCase):
    @staticmethod
    def data(counts, *, noise=0.025, seed=91):
        import numpy as np
        rng = np.random.default_rng(seed)
        pieces = []
        for index, count in enumerate(counts):
            center = np.zeros(8)
            center[index] = 1
            samples = center + rng.normal(0, noise, (count, 8))
            pieces.append(samples / np.linalg.norm(samples, axis=1, keepdims=True))
        vectors = np.concatenate(pieces).astype("float32")
        windows = [(i * 32000, i * 32000 + 28800, i) for i in range(len(vectors))]
        return windows, vectors

    @staticmethod
    def major(labels):
        import numpy as np
        return int(np.bincount(labels).argmax())

    def test_two_main_voices_survive_small_far_outlier_group(self):
        import numpy as np
        windows, vectors = self.data([110, 40])
        rng = np.random.default_rng(17)
        outliers = -vectors[:4] + rng.normal(0, 0.01, (4, 8))
        vectors = np.concatenate([vectors, outliers]).astype("float32")
        windows.extend((i * 32000, i * 32000 + 28800, i) for i in range(150, 154))
        labels, uncertain = cluster(windows, vectors, 2)
        first, second = self.major(labels[:110]), self.major(labels[110:150])
        self.assertNotEqual(first, second)
        self.assertGreater(float(np.mean(labels[:110] == first)), 0.95)
        self.assertGreater(float(np.mean(labels[110:150] == second)), 0.95)
        self.assertTrue(bool(uncertain[150:].all()))

    def test_three_unequal_speakers_keep_the_small_person(self):
        import numpy as np
        windows, vectors = self.data([120, 25, 8])
        labels, _ = cluster(windows, vectors, 3)
        groups = [labels[:120], labels[120:145], labels[145:]]
        self.assertEqual(len({self.major(group) for group in groups}), 3)
        self.assertEqual(sorted(np.bincount(labels).tolist()), [8, 25, 120])

    def test_explicit_two_speakers_do_not_force_equal_sizes(self):
        import numpy as np
        windows, vectors = self.data([160, 6])
        labels, _ = cluster(windows, vectors, 2)
        self.assertEqual(sorted(np.bincount(labels).tolist()), [6, 160])

    def test_automatic_count_finds_uneven_two_voices(self):
        import numpy as np
        windows, vectors = self.data([90, 10])
        labels, uncertain = cluster(windows, vectors, 0)
        self.assertEqual(sorted(np.bincount(labels).tolist()), [10, 90])
        self.assertTrue(bool(uncertain.all()))

    def test_automatic_count_does_not_split_one_consistent_voice(self):
        windows, vectors = self.data([40], noise=0.01)
        labels, _ = cluster(windows, vectors, 0)
        self.assertEqual(len(set(labels.tolist())), 1)

    def test_identical_voice_features_cannot_claim_two_people(self):
        import numpy as np
        windows, vectors = self.data([8])
        vectors[:] = np.array([1, 0, 0, 0, 0, 0, 0, 0])
        with self.assertRaisesRegex(ValueError, "不足以区分"):
            cluster(windows, vectors, 2)

    def test_nearly_identical_voice_groups_stay_uncertain(self):
        import numpy as np
        windows, vectors = self.data([40], noise=0.002)
        diagnostics = {}
        labels, uncertain = cluster(windows, vectors, 2, diagnostics=diagnostics)
        self.assertEqual(len(labels), 40)
        self.assertTrue(np.isfinite(labels).all())
        self.assertTrue(bool(uncertain.all()))
        self.assertTrue(diagnostics["warnings"])

    def test_invalid_features_are_rejected_before_grouping(self):
        import numpy as np
        windows, vectors = self.data([4, 4])
        vectors[0] = 0
        with self.assertRaisesRegex(ValueError, "无效"):
            cluster(windows, vectors, 2)
        vectors[0] = np.nan
        with self.assertRaisesRegex(ValueError, "不完整"):
            cluster(windows, vectors, 2)

    def test_repeated_runs_are_deterministic(self):
        import numpy as np
        windows, vectors = self.data([35, 12])
        first = cluster(windows, vectors, 2)
        second = cluster(windows, vectors, 2)
        self.assertTrue(np.array_equal(first[0], second[0]))
        self.assertTrue(np.array_equal(first[1], second[1]))

    def test_training_size_is_bounded_for_long_recordings(self):
        import numpy as np
        windows, vectors = self.data([1900, 100])
        diagnostics = {}
        labels, _ = cluster(windows, vectors, 2, diagnostics=diagnostics)
        self.assertEqual(diagnostics["training_windows"], 1800)
        self.assertEqual(len(labels), 2000)
        self.assertEqual(sorted(np.bincount(labels).tolist()), [100, 1900])


if __name__ == "__main__":
    unittest.main()
