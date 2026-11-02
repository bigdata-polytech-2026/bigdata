import unittest

from analysis.temporal.pipeline import classify_scenario


class ScenarioTest(unittest.TestCase):
    def test_scenarios(self):
        self.assertEqual(classify_scenario([0, 0]), "always_current")
        self.assertEqual(classify_scenario([0, 1, 3]), "accumulation")
        self.assertEqual(classify_scenario([2, 1, 0]), "catch_up")
        self.assertEqual(classify_scenario([2, 0, 1]), "persistent_lag")
        self.assertEqual(classify_scenario([]), "other")
