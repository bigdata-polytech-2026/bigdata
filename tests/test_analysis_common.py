from __future__ import annotations

import unittest

from analysis.version_constraints.pipeline import _cramers_v, _cluster_risk_differences


class AnalysisStatisticsTest(unittest.TestCase):
    def test_cramers_v_detects_no_and_complete_association(self):
        self.assertAlmostEqual(_cramers_v([[10, 10], [20, 20]])[2], 0.0)
        self.assertGreater(_cramers_v([[20, 0], [0, 20]])[2], 0.9)

    def test_cluster_risk_difference_uses_caret_reference(self):
        rows = [
            ("a", "caret", 10, 2), ("a", "exact", 10, 8),
            ("b", "caret", 10, 4), ("b", "exact", 10, 6),
        ]
        result = _cluster_risk_differences(rows, repetitions=20, seed=7)
        self.assertAlmostEqual(result["exact"]["risk_difference_vs_caret"], 0.4)
        self.assertEqual(result["exact"]["bootstrap_repetitions"], 20)


if __name__ == "__main__":
    unittest.main()
