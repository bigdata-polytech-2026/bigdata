from __future__ import annotations

import unittest

from analysis.common import spearman_with_bootstrap
from analysis.version_constraints.pipeline import _age_gradient, _cramers_v, _cluster_risk_differences


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

    def test_spearman_aggregates_repeated_package_rows(self):
        result = spearman_with_bootstrap(
            [1, 3, 10, 12, 14, 20],
            [2, 4, 8, 10, 12, 30],
            ["a", "a", "b", "b", "b", "c"],
            repetitions=0,
            seed=7,
        )
        self.assertEqual(result["n"], 3)
        self.assertEqual(result["unit"], "consumer_package")
        self.assertAlmostEqual(result["rho"], 1.0)

    def test_age_gradient_aggregates_package_cluster_scores(self):
        rows = []
        for index in range(20):
            package = f"package-{index}"
            age = 1.0 + index / 10
            rows.append((package, "caret", 0.1 * index, age, 2.0 + index, 3 + index, 4))
            rows.append((package, "exact", 0.2 * index, age, 2.0 + index, 3 + index, 2))

        class FakeResult:
            def fetchall(self):
                return rows

        class FakeDatabase:
            def execute(self, _query):
                return FakeResult()

        result = _age_gradient(FakeDatabase())
        self.assertEqual(len(result), 12)
        self.assertTrue(all(row[-2:] == (40, 20) for row in result))


if __name__ == "__main__":
    unittest.main()
