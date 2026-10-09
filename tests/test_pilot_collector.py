from __future__ import annotations

import gzip
import json
import tarfile
import tempfile
import unittest
from pathlib import Path

from collector.pilot.collector import (
    PilotCollector,
    PilotSummary,
    _state_key,
    build_version_index,
    iter_selected_versions,
)


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value) + "\n", encoding="utf-8")


class PilotCollectorTest(unittest.TestCase):
    def test_disk_index_selects_oldest_middle_and_newest_versions(self):
        with tempfile.TemporaryDirectory() as temporary:
            landing = Path(temporary) / "landing"
            version_path = landing / "normalized" / "v1" / "package_version" / "date" / "example.jsonl"
            version_path.parent.mkdir(parents=True)
            with version_path.open("w", encoding="utf-8") as handle:
                for number in range(1, 6):
                    handle.write(json.dumps({"package_name": "example", "version": f"{number}.0.0", "published_at": f"2020-01-0{number}T00:00:00Z"}) + "\n")
            write_json(
                landing / "state" / "npm" / f"{_state_key('example')}.json",
                {"requested_name": "example", "requirements": 7, "normalized_paths": [version_path.relative_to(landing).as_posix()]},
            )
            database = landing / "inputs" / "versions.sqlite"
            selected = landing / "inputs" / "selected.txt"

            stats = build_version_index(landing, ["example"], 3, database, selected)

            self.assertEqual((stats.packages, stats.package_versions, stats.dependency_requirements, stats.selected_versions), (1, 5, 7, 3))
            self.assertEqual(list(iter_selected_versions(database)), [("example", "1.0.0"), ("example", "3.0.0"), ("example", "5.0.0")])

    def test_snapshot_contains_verifiable_raw_audit_and_normalized_shards(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_root = Path(temporary) / "data"
            collector = PilotCollector(data_root, dataset_id="pilot-test", package_limit=1, versions_per_package=1, shard_size_mb=1)
            landing = collector.landing_dir
            raw_npm = landing / "raw" / "npm" / "date" / "npm.json"
            raw_npm.parent.mkdir(parents=True)
            raw_npm.write_bytes(b'{"name":"example"}\n')
            package_path = landing / "normalized" / "v1" / "package" / "date" / "npm.jsonl"
            version_path = landing / "normalized" / "v1" / "package_version" / "date" / "npm.jsonl"
            requirement_path = landing / "normalized" / "v1" / "dependency_requirement" / "date" / "npm.jsonl"
            write_json(package_path, {"package_name": "example"})
            write_json(version_path, {"package_name": "example", "version": "1.0.0", "published_at": "2020-01-01T00:00:00Z"})
            write_json(requirement_path, {"source_package": "example", "source_version": "1.0.0"})
            npm_state = landing / "state" / "npm" / f"{_state_key('example')}.json"
            write_json(npm_state, {
                "requested_name": "example", "requirements": 1, "raw_path": raw_npm.relative_to(landing).as_posix(),
                "normalized_paths": [path.relative_to(landing).as_posix() for path in (package_path, version_path, requirement_path)],
            })
            packages_path = landing / "inputs" / "packages.txt"
            packages_path.parent.mkdir(parents=True)
            packages_path.write_text("example\n")
            index_path = landing / "inputs" / "versions.sqlite"
            selected_path = landing / "inputs" / "selected.txt"
            build_version_index(landing, ["example"], 1, index_path, selected_path)

            for source, entity in (("depsdev", "dependency_relation"), ("osv", "vulnerability")):
                raw_path = landing / "raw" / source / "date" / f"{source}.json"
                raw_path.parent.mkdir(parents=True)
                raw_path.write_bytes(b"{}\n")
                normalized_path = landing / "normalized" / ("v1" if source == "depsdev" else "v2") / entity / "date" / f"{source}.jsonl"
                write_json(normalized_path, {"source": source})
                state = {
                    "package_name": "example", "version": "1.0.0", "raw_path": raw_path.relative_to(landing).as_posix(),
                    "normalized_path": normalized_path.relative_to(landing).as_posix(),
                }
                if source == "depsdev":
                    state.update({"requested_package": "example", "requested_version": "1.0.0"})
                else:
                    state["raw_pages"] = [{"raw_path": state["raw_path"], "raw_sha256": "test"}]
                write_json(landing / "state" / source / f"{_state_key('example', '1.0.0')}.json", state)
                manifest_path = landing / "manifests" / source / "run.json"
                write_json(manifest_path, {"source": source})

            npm_manifest = landing / "manifests" / "npm" / "run.json"
            write_json(npm_manifest, {"source": "npm"})
            (landing / "reports").mkdir(parents=True)
            (landing / "reports" / "depsdev-failures.txt").write_text("")
            (landing / "reports" / "osv-failures.txt").write_text("")
            summary = PilotSummary(
                run_id="test", dataset_id="pilot-test", status="complete", package_target=1, packages=1,
                package_versions=1, dependency_requirements=1, selected_versions=1, depsdev_successes=1,
                depsdev_success_rate=1.0, dependency_relations=1, osv_checked=1, osv_success_rate=1.0, criteria_met=True,
                snapshot_path="datasets/pilot-test", manifest_path="datasets/pilot-test/manifest.json", report_path="datasets/pilot-test/report.md",
            )
            manifests = {
                "npm": [npm_manifest.relative_to(landing).as_posix()],
                "depsdev": ["manifests/depsdev/run.json"],
                "osv": ["manifests/osv/run.json"],
            }

            collector._build_snapshot(summary, manifests, ["example"], index_path, packages_path, selected_path, 0.0)

            manifest = json.loads((collector.snapshot_dir / "manifest.json").read_text())
            self.assertTrue(manifest["immutable"])
            self.assertTrue(manifest["shards"]["raw"]["npm"])
            self.assertTrue(manifest["shards"]["audit"]["npm"])
            package_shard = collector.snapshot_dir / manifest["shards"]["normalized"]["package"][0]["path"]
            with gzip.open(package_shard, "rt", encoding="utf-8") as handle:
                self.assertEqual(json.loads(handle.readline())["package_name"], "example")
            raw_shard = collector.snapshot_dir / manifest["shards"]["raw"]["npm"][0]["path"]
            with tarfile.open(raw_shard, "r:gz") as archive:
                self.assertIn("raw/npm/date/npm.json", archive.getnames())


if __name__ == "__main__":
    unittest.main()
