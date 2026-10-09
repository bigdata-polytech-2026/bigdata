from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from collector.npm.collector import HttpResponse, NpmCollector, normalize_timestamp


PACKUMENT = b'''{
  "_id": "example",
  "name": "example",
  "repository": {"type": "git", "url": "git+https://example.invalid/example.git"},
  "dist-tags": {"latest": "2.0.0"},
  "time": {
    "created": "2020-01-01T03:00:00+03:00",
    "modified": "2020-02-01T00:00:00.123456789Z",
    "1.0.0": "2020-01-01T00:00:00.000Z",
    "2.0.0": "2020-02-01T00:00:00.000Z"
  },
  "versions": {
    "1.0.0": {
      "name": "example",
      "version": "1.0.0",
      "dependencies": {"alpha": "^1.0.0"},
      "devDependencies": {"test-tool": "~2.0.0"},
      "repository": "https://example.invalid/v1"
    },
    "2.0.0": {
      "name": "example",
      "version": "2.0.0",
      "optionalDependencies": {"native-addon": "*"},
      "deprecated": "use example-next"
    }
  },
  "custom_field": {"must": "remain in raw"}
}'''


class FakeClient:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def get(self, url):
        self.calls.append(url)
        value = self.responses[url]
        if isinstance(value, Exception):
            raise value
        return value


class NpmCollectorTest(unittest.TestCase):
    def test_timestamp_normalization_preserves_fraction(self):
        self.assertEqual(normalize_timestamp("2020-02-01T00:00:00.123456789Z"), "2020-02-01T00:00:00.123456789Z")
        self.assertEqual(normalize_timestamp("2020-01-01T03:00:00+03:00"), "2020-01-01T00:00:00Z")
        self.assertIsNone(normalize_timestamp("not-a-timestamp"))

    def test_raw_normalized_error_isolation_and_resume(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data"
            base = "https://registry.example"
            client = FakeClient(
                {
                    f"{base}/example": HttpResponse(
                        url=f"{base}/example",
                        status=200,
                        body=PACKUMENT,
                        headers={"etag": "test-etag"},
                    ),
                    f"{base}/broken": RuntimeError("simulated package failure"),
                }
            )
            collector = NpmCollector(data_dir=data_dir, registry_url=base, workers=2, client=client)
            summary = collector.run(limit=2, package_names=["example", "broken"])

            self.assertEqual(summary.downloaded, 1)
            self.assertEqual(summary.failed, 1)
            self.assertEqual(summary.available, 1)
            raw_files = list((data_dir / "raw" / "npm").rglob("*.json"))
            self.assertEqual(len(raw_files), 1)
            self.assertEqual(raw_files[0].read_bytes(), PACKUMENT)

            version_file = next((data_dir / "normalized" / "v1" / "package_version").rglob("*.jsonl"))
            versions = [json.loads(line) for line in version_file.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(versions), 2)
            self.assertTrue(versions[1]["is_deprecated"])
            self.assertEqual(versions[1]["deprecated_message"], "use example-next")
            self.assertEqual(versions[0]["repository"], "https://example.invalid/v1")

            requirements_file = next(
                (data_dir / "normalized" / "v1" / "dependency_requirement").rglob("*.jsonl")
            )
            requirements = [json.loads(line) for line in requirements_file.read_text(encoding="utf-8").splitlines()]
            self.assertEqual({item["dependency_type"] for item in requirements}, {"dependencies", "devDependencies", "optionalDependencies"})

            log_text = (data_dir / summary.log_path).read_text(encoding="utf-8")
            self.assertIn('"event":"package_failed"', log_text)
            self.assertIn("simulated package failure", log_text)

            no_network = FakeClient({})
            second = NpmCollector(data_dir=data_dir, registry_url=base, workers=1, client=no_network)
            resumed = second.run(limit=1, package_names=["example"])
            self.assertEqual(resumed.skipped, 1)
            self.assertEqual(resumed.available, 1)
            self.assertEqual(no_network.calls, [])

            raw_files[0].write_bytes(b"{}\n")
            repaired = NpmCollector(data_dir=data_dir, registry_url=base, workers=1, client=client).run(limit=1, package_names=["example"])
            self.assertEqual((repaired.downloaded, repaired.skipped, repaired.available), (1, 0, 1))
            self.assertEqual(len(list((data_dir / "raw" / "npm").rglob("*.json"))), 2)


if __name__ == "__main__":
    unittest.main()
