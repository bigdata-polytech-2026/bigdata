from __future__ import annotations

import gzip
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from processing.security.pipeline import run


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def record(osv_id="GHSA-test", package="lib", *, fixed="2.0.0"):
    return {"schema_version": "2.0.0", "source": "osv", "osv_id": osv_id, "affected_index": 0,
            "package_name": package, "ecosystem": "npm", "aliases": ["CVE-1"],
            "affected": {"versions": [], "ranges": [{"type": "SEMVER", "events": [{"introduced": "1.0.0"}, {"fixed": fixed}]}]},
            "fixed_versions": [fixed], "severity": [{"type": "CVSS_V3", "score": "vector", "source": None}],
            "provenance": {"snapshot_id": "x"}}


class SecurityPipelineTest(unittest.TestCase):
    def fixture(self, root: Path, records):
        dataset, normalized = root / "pilot", root / "normalized"
        dataset.mkdir(); (dataset / "normalized/vulnerability").mkdir(parents=True)
        shard = dataset / "normalized/vulnerability/part-00000.jsonl.gz"
        with gzip.open(shard, "wt", encoding="utf-8") as stream:
            for value in records: stream.write(json.dumps(value) + "\n")
        (dataset / "manifest.json").write_text(json.dumps({"shards": {"normalized": {"vulnerability": [{"path": "normalized/vulnerability/part-00000.jsonl.gz", "sha256": sha(shard), "rows": len(records)}]}}}))
        (normalized / "versions").mkdir(parents=True)
        pq.write_table(pa.Table.from_pylist([{"package_name": "lib", "version": "1.0.0"}, {"package_name": "lib", "version": "2.0.0"}, {"package_name": "other", "version": "1.0.0"}]), normalized / "versions/part-00000.parquet")
        return dataset, normalized

    def test_builds_features_and_separates_unmatched_records(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); dataset, normalized = self.fixture(root, [record(), record(package="missing")])
            report = run(dataset, normalized, root / "security")
            self.assertEqual(report["vulnerable_package_versions"], 1)
            self.assertEqual(report["osv_records_unmapped"], 1)
            self.assertEqual(report["mapping_success_fraction"], 0.5)
            rows = pq.read_table(root / "security/security_features.parquet").to_pylist()
            vulnerable = next(row for row in rows if row["version"] == "1.0.0" and row["package_name"] == "lib")
            self.assertTrue(vulnerable["is_vulnerable"]); self.assertEqual(json.loads(vulnerable["osv_ids"]), ["GHSA-test"])
            self.assertTrue(vulnerable["has_fixed_version"])
            fixed = next(row for row in rows if row["version"] == "2.0.0" and row["package_name"] == "lib")
            self.assertFalse(fixed["is_vulnerable"])
            errors = pq.read_table(root / "security/mapping_errors.parquet").to_pylist()
            self.assertEqual(errors[0]["error_reason"], "unmatched_osv_package")
            self.assertEqual(run(dataset, normalized, root / "security"), report)
            with (root / "security/security_features.parquet").open("ab") as stream:
                stream.write(b"corruption")
            with self.assertRaisesRegex(ValueError, "security output SHA-256 mismatch"):
                run(dataset, normalized, root / "security")

    def test_invalid_range_is_saved_as_mapping_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); bad = record(); bad["affected"]["ranges"][0]["events"][1]["fixed"] = "not-a-version"
            dataset, normalized = self.fixture(root, [bad])
            report = run(dataset, normalized, root / "security")
            self.assertEqual(report["mapping_errors"], {"invalid_range_boundary": 2})
            rows = pq.read_table(root / "security/security_features.parquet").to_pylist()
            self.assertTrue(all(not row["is_vulnerable"] for row in rows))


if __name__ == "__main__": unittest.main()
