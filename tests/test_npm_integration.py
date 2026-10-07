from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from collector.npm.collector import NpmCollector


@unittest.skipUnless(os.environ.get("RUN_NPM_INTEGRATION") == "1", "set RUN_NPM_INTEGRATION=1 for live npm tests")
class NpmIntegrationTest(unittest.TestCase):
    def test_known_packages(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data"
            packages = ["express", "lodash", "request"]
            collector = NpmCollector(data_dir=data_dir, workers=3, timeout=30.0, retries=2)
            summary = collector.run(limit=len(packages), package_names=packages)

            self.assertEqual(summary.available, len(packages))
            self.assertEqual(summary.failed, 0)
            self.assertEqual(len(list((data_dir / "raw" / "npm").rglob("*.json"))), len(packages))

            package_records = []
            for path in (data_dir / "normalized" / "v1" / "package").rglob("*.jsonl"):
                package_records.extend(json.loads(line) for line in path.read_text(encoding="utf-8").splitlines())
            self.assertEqual({record["package_name"] for record in package_records}, set(packages))
            self.assertTrue(all(record["repository"] is not None for record in package_records))

            version_records = []
            for path in (data_dir / "normalized" / "v1" / "package_version").rglob("*.jsonl"):
                version_records.extend(json.loads(line) for line in path.read_text(encoding="utf-8").splitlines())
            self.assertTrue(all(record["published_at"] is not None for record in version_records))
            self.assertTrue(any(record["package_name"] == "request" and record["is_deprecated"] for record in version_records))

            requirements = list((data_dir / "normalized" / "v1" / "dependency_requirement").rglob("*.jsonl"))
            self.assertTrue(requirements)
            self.assertGreater(sum(path.stat().st_size for path in requirements), 0)


if __name__ == "__main__":
    unittest.main()
