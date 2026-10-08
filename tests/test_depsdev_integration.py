from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from collector.depsdev.collector import DepsDevCollector


@unittest.skipUnless(os.environ.get("RUN_DEPSDEV_INTEGRATION") == "1", "set RUN_DEPSDEV_INTEGRATION=1 for live deps.dev tests")
class DepsDevIntegrationTest(unittest.TestCase):
    def test_known_npm_versions(self):
        package_versions = [("react", "18.2.0"), ("lodash", "4.17.21"), ("@colors/colors", "1.5.0")]
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data"
            summary = DepsDevCollector(data_dir, workers=3, timeout=30.0, retries=2).run(package_versions)
            self.assertEqual((summary.target, summary.processed, summary.api_successes, summary.api_failures), (3, 3, 3, 0))
            self.assertEqual(len(list((data_dir / "raw" / "depsdev").rglob("*.json"))), 3)
            manifest = json.loads(next((data_dir / "manifests" / "depsdev").glob("*.json")).read_text(encoding="utf-8"))
            self.assertEqual([item["package_name"] for item in manifest["results"]], [item[0] for item in package_versions])
            react = next(item for item in manifest["results"] if item["package_name"] == "react")
            self.assertGreaterEqual(react["edges"], 2)
            self.assertEqual(react["direct_edges"], 1)


if __name__ == "__main__":
    unittest.main()
