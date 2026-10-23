from __future__ import annotations

import unittest
from processing.temporal.pipeline import observation_times

T0 = "2026-01-01T00:00:00Z"
T1 = "2026-01-02T00:00:00Z"
T2 = "2026-01-03T00:00:00Z"
T3 = "2026-01-04T00:00:00Z"

class TemporalBoundaryTest(unittest.TestCase):
    def test_manual_time_boundaries(self):
        cases = [(T0, [], T3, 12, [T0]), (T0, [T0], T3, 12, [T0]),
                 (T0, [T1], T3, 12, [T0, T1]), (T0, [T1,T2,T3], T2, 12, [T0,T1,T2]),
                 (T0, [T1,T2], T0, 12, [T0]), (T1, [T0,T1,T2], T3, 12, [T1,T2]),
                 (T0, [T2,T1,T1], T3, 12, [T0,T1,T2]), (T0, [T1,T2,T3], T3, 2, [T0,T3]),
                 (T0, [T1,T2,T3], T3, 3, [T0,T1,T3]), (T0, ["2025-12-31T23:59:59Z",T3], T2, 12, [T0])]
        for consumer, releases, ceiling, limit, expected in cases:
            with self.subTest(releases=releases, limit=limit):
                self.assertEqual(observation_times(consumer, releases, ceiling, limit), expected)
