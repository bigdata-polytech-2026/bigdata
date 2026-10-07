from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from collector.osv.collector import HttpResponse, OsvCollector


VULNERABLE = {
    "vulns": [{
        "id": "GHSA-test-1234-5678", "published": "2021-01-01T00:00:00Z", "modified": "2021-01-02T03:00:00+03:00", "aliases": ["CVE-2021-0001"],
        "severity": [{"type": "CVSS_V3", "score": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"}],
        "affected": [{"package": {"ecosystem": "npm", "name": "vulnerable-package", "purl": "pkg:npm/vulnerable-package"}, "ranges": [{"type": "SEMVER", "events": [{"introduced": "0"}, {"fixed": "2.0.0"}]}]},
                     {"package": {"ecosystem": "PyPI", "name": "ignore-me"}, "versions": ["1.0"]}]
    }]
}


class FakeClient:
    def __init__(self, values): self.values, self.calls = values, []
    def post_json(self, url, value):
        self.calls.append((url, value)); result = self.values[(value["package"]["name"], value["version"])]
        if isinstance(result, Exception): raise result
        return HttpResponse(url, 200, json.dumps(result).encode(), {})


class OsvCollectorTest(unittest.TestCase):
    def test_vulnerable_and_not_vulnerable_versions_are_distinct(self):
        with tempfile.TemporaryDirectory() as temporary:
            client = FakeClient({("vulnerable-package", "1.0.0"): VULNERABLE, ("safe-package", "1.0.0"): {}})
            data_dir = Path(temporary) / "data"
            summary = OsvCollector(data_dir, api_url="https://osv.example", client=client).run([("vulnerable-package", "1.0.0"), ("safe-package", "1.0.0")])
            self.assertEqual((summary.checked, summary.versions_with_vulnerabilities, summary.unique_osv_records, summary.api_errors), (2, 1, 1, 0))
            manifests = list((data_dir / "manifests" / "osv").glob("*.json")); manifest = json.loads(manifests[0].read_text())
            self.assertEqual([item["status"] for item in manifest["results"]], ["vulnerabilities_found", "no_vulnerabilities"])
            raw_files = list((data_dir / "raw" / "osv").rglob("*.json")); self.assertEqual(len(raw_files), 2)
            records = [json.loads(line) for path in (data_dir / "normalized" / "v1" / "vulnerability").rglob("*.jsonl") for line in path.read_text().splitlines()]
            self.assertEqual(len(records), 1); record = records[0]
            self.assertEqual(record["osv_id"], "GHSA-test-1234-5678")
            self.assertEqual(record["ecosystem"], "npm")
            self.assertEqual(record["aliases"], ["CVE-2021-0001"])
            self.assertEqual(record["modified_at"], "2021-01-02T00:00:00Z")
            self.assertEqual(record["fixed_versions"], ["2.0.0"])
            self.assertEqual(record["severity"][0]["type"], "CVSS_V3")

    def test_api_error_is_logged_and_does_not_abort_run(self):
        with tempfile.TemporaryDirectory() as temporary:
            client = FakeClient({("broken", "1.0.0"): RuntimeError("offline")})
            summary = OsvCollector(Path(temporary) / "data", client=client).run([("broken", "1.0.0")])
            self.assertEqual((summary.checked, summary.api_errors), (0, 1))


if __name__ == "__main__": unittest.main()
