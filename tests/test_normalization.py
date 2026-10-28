from __future__ import annotations

import hashlib
import gzip
import io
import json
import shutil
import tarfile
import tempfile
import unittest
import uuid
from pathlib import Path

import pyarrow.parquet as pq

from processing.normalization.pipeline import analyze, digest, run as normalize


def run(root):
    return normalize(root / "source", root / "normalized/parquet")


class NormalizationTest(unittest.TestCase):
    def fixture(self, root, *, bad_hash=False, duplicate=False, bad_root=False,
                identical_duplicate=False, legacy=False, legacy_provenance=True,
                empty_graph=False, extra_versions=False):
        root = root / "source"
        root.mkdir()
        manifest = {"shards": {"raw": {}, "audit": {}}}
        app = {"name": "@scope/app", "versions": {"1.0.0": {
            "dependencies": {"lib": "^1.0.0", "external": "*", "alias": "npm:lib@^1.0.0"},
            "optionalDependencies": {"lib": "~1.0.0"},
        }}, "time": {"1.0.0": "2020-01-01T00:00:00.123456789Z"},
            "repository": {"url": "git://example"}, "dist-tags": {"latest": "1.0.0"}}
        lib = {"name": "lib", "versions": {"1.0.0": {}}, "time": {}}
        if extra_versions:
            for version in ("01.0.0", "1.0.0-rc.1"):
                app["versions"][version] = app["versions"]["1.0.0"]
                app["time"][version] = app["time"]["1.0.0"]
        nodes = [{"versionKey": {"system": "NPM", "name": name, "version": "1.0.0"}}
                 for name in ("@scope/app", "lib", "external")]
        graph = {"nodes": nodes, "edges": [{"fromNode": 0, "toNode": 1}, {"fromNode": 1, "toNode": 2}]}
        if empty_graph:
            graph["edges"] = []
        npm_documents = [app, lib]
        if duplicate or identical_duplicate:
            duplicate_app = json.loads(json.dumps(app))
            if duplicate:
                duplicate_app["description"] = "conflicting snapshot"
            npm_documents.append(duplicate_app)
        for source, documents in (("npm", npm_documents), ("depsdev", [graph])):
            raw_entries, states = [], []
            for number, document in enumerate(documents):
                snapshot = str(uuid.uuid4())
                raw_path = f"raw/{source}/{snapshot}.json"
                payload = json.dumps(document).encode()
                state = {"snapshot_id": snapshot, "request_url": "https://example.test/source",
                         "retrieved_at": "2026-01-01T00:00:00Z", "raw_path": raw_path,
                         "raw_sha256": "0" * 64 if bad_hash else hashlib.sha256(payload).hexdigest(),
                         "parser_version": "original-collector", "http_status": 200}
                if source == "npm":
                    state["requested_name"] = document["name"]
                else:
                    state.update(requested_package="wrong" if bad_root else "@scope/app", requested_version="1.0.0")
                    if legacy:
                        if legacy_provenance:
                            from collector.depsdev.collector import normalize_graph
                            provenance = {key: state[key] for key in ("snapshot_id", "request_url", "retrieved_at", "raw_path", "raw_sha256", "parser_version")}
                            records = normalize_graph(document, provenance).records
                            path = root / "normalized/dependency_relation/part-00000.jsonl.gz"
                            path.parent.mkdir(parents=True)
                            with gzip.open(path, "wt") as stream:
                                for record in records:
                                    stream.write(json.dumps(record) + "\n")
                            manifest["shards"]["normalized"] = {"dependency_relation": [{
                                "path": path.relative_to(root).as_posix(), "sha256": digest(path), "rows": len(records)}]}
                        del state["retrieved_at"]
                raw_entries.append((raw_path, payload))
                states.append((f"state/{source}/{number}.json", json.dumps(state).encode()))
            for group, entries in (("raw", raw_entries), ("audit", states)):
                path = root / group / source / "part-00000.tar.gz"
                path.parent.mkdir(parents=True)
                with tarfile.open(path, "w:gz") as archive:
                    for name, payload in entries:
                        member = tarfile.TarInfo(name)
                        member.size = len(payload)
                        archive.addfile(member, io.BytesIO(payload))
                manifest["shards"][group][source] = [{"path": path.relative_to(root).as_posix(),
                    "sha256": digest(path), "files": len(entries)}]
        (root / "manifest.json").write_text(json.dumps(manifest))

    def test_raw_to_parquet_matching_provenance_and_resume(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root)
            report = run(root)
            self.assertEqual(report["counts"], {"packages": 2, "versions": 2,
                "dependency_requirements": 4, "dependency_relations": 2})
            self.assertEqual(report["matching"]["relations"]["unmatched_fraction"], 0.5)
            self.assertEqual(report["matching"]["requirement_dependency_packages"]["unmatched"], 2)
            self.assertTrue(all(v == 0 for v in report["integrity_violations"].values()))
            output = root / "normalized/parquet"
            versions = pq.read_table(output / "versions").to_pylist()
            self.assertEqual(versions[0]["published_at"], "2020-01-01T00:00:00.123456789Z")
            package = pq.read_table(output / "packages").to_pylist()[0]
            self.assertEqual(json.loads(package["repository"]), {"url": "git://example"})
            self.assertEqual(json.loads(package["dist_tags"]), {"latest": "1.0.0"})
            self.assertIsNone(versions[1]["published_at"])
            self.assertTrue(versions[0]["provenance"]["parser_version"].startswith("pilot-normalization@"))
            requirements = pq.read_table(output / "technical_lag_inputs.parquet").to_pylist()
            app_lib = next(r for r in requirements if r["dependency_name"] == "lib" and r["dependency_type"] == "dependencies")
            self.assertTrue(app_lib["shadowed_by_optional"])
            self.assertEqual(app_lib["preparation_exclusion_reason"], "optional_dependency_override")
            optional_lib = next(r for r in requirements if r["dependency_type"] == "optionalDependencies")
            self.assertFalse(optional_lib["shadowed_by_optional"])
            alias = next(r for r in requirements if r["dependency_name"] == "alias")
            self.assertEqual(alias["preparation_exclusion_reason"], "unsupported_requirement")
            before = {p: digest(p) for p in output.rglob("*") if p.is_file()}
            self.assertEqual(run(root), report)
            self.assertEqual(before, {p: digest(p) for p in before})

    def test_corrupt_raw_does_not_publish_partial_dataset(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root, bad_hash=True)
            with self.assertRaisesRegex(ValueError, "raw SHA-256 mismatch"):
                run(root)
            self.assertFalse((root / "normalized/parquet").exists())
            self.assertEqual(list((root / "normalized").iterdir()), [])

    def test_duplicate_logical_keys_fail(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root, duplicate=True)
            with self.assertRaisesRegex(ValueError, "conflicting npm snapshots"):
                run(root)
            self.assertFalse((root / "normalized/parquet").exists())

    def test_identical_duplicate_snapshot_is_counted_and_skipped(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root, identical_duplicate=True)
            report = run(root)
            self.assertEqual(report["counts"]["packages"], 2)
            self.assertEqual(report["raw_statistics"]["npm_duplicate_snapshots_skipped"], 1)

    def test_graph_identity_must_match_requested_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root, bad_root=True)
            with self.assertRaisesRegex(ValueError, "root identity mismatch"):
                run(root)

    def test_resume_rejects_corrupt_parquet(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root)
            run(root)
            output = root / "normalized/parquet/packages/part-00000.parquet"
            with output.open("ab") as stream:
                stream.write(b"corruption")
            with self.assertRaisesRegex(ValueError, "output SHA-256 mismatch"):
                run(root)

    def test_independent_rebuild_has_identical_parquet_rows(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first, second = root / "first", root / "second"
            first.mkdir()
            self.fixture(first)
            shutil.copytree(first, second)
            self.assertEqual(run(first), run(second))
            for path in (first / "normalized/parquet").rglob("*.parquet"):
                other = second / path.relative_to(first)
                self.assertTrue(pq.read_table(path).equals(pq.read_table(other)))

    def test_legacy_state_recovers_exact_time_from_verified_provenance(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root, legacy=True)
            report = run(root)
            self.assertEqual(report["counts"]["dependency_relations"], 2)
            rows = pq.read_table(root / "normalized/parquet/dependency_relations").to_pylist()
            self.assertEqual(rows[0]["provenance"]["retrieved_at"], "2026-01-01T00:00:00Z")

    def test_legacy_nonempty_graph_without_timestamp_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root, legacy=True, legacy_provenance=False)
            with self.assertRaisesRegex(ValueError, "no exact retrieved_at"):
                run(root)

    def test_empty_legacy_graph_has_typed_empty_relation_table(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root, legacy=True, legacy_provenance=False, empty_graph=True)
            report = run(root)
            self.assertEqual(report["counts"]["dependency_relations"], 0)
            self.assertEqual(report["raw_statistics"]["empty_graphs_without_retrieval_time"], 1)
            table = pq.read_table(root / "normalized/parquet/dependency_relations")
            self.assertEqual(table.num_rows, 0)
            self.assertIn("resolved_version", table.column_names)

    def test_resume_rechecks_input_archive_hashes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root)
            run(root)
            with (root / "source/raw/npm/part-00000.tar.gz").open("ab") as stream:
                stream.write(b"corruption")
            with self.assertRaisesRegex(ValueError, "input archive SHA-256 mismatch"):
                run(root)

    def test_integrity_rejects_requirements_with_missing_source_version(self):
        import duckdb
        import pyarrow.compute as pc

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root)
            run(root)
            output = root / "normalized/parquet"
            path = output / "versions/part-00000.parquet"
            versions = pq.read_table(path)
            pq.write_table(versions.filter(pc.equal(versions["package_name"], "lib")), path)
            with self.assertRaisesRegex(ValueError, "requirements_without_source_version"):
                analyze(output, duckdb)

    def test_input_snapshot_is_immutable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root)
            source = root / "source"
            before = {p: digest(p) for p in source.rglob("*") if p.is_file()}
            run(root)
            self.assertEqual(before, {p: digest(p) for p in source.rglob("*") if p.is_file()})
            with self.assertRaisesRegex(ValueError, "immutable input dataset"):
                normalize(source, source / "normalized/parquet")

    def test_integrity_rejects_null_required_provenance(self):
        import duckdb
        import pyarrow as pa

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root)
            run(root)
            output = root / "normalized/parquet"
            path = output / "packages/part-00000.parquet"
            table = pq.read_table(path)
            rows = table.to_pylist()
            rows[0]["provenance"]["raw_sha256"] = None
            pq.write_table(pa.Table.from_pylist(rows, schema=table.schema), path)
            with self.assertRaisesRegex(ValueError, "packages_invalid_common"):
                analyze(output, duckdb)

    def test_semver_quality_flags_invalid_and_prerelease_without_coercion(self):
        import subprocess
        helper = Path(__file__).parents[1] / "processing/normalization/version-quality.cjs"
        result = subprocess.run(["node", str(helper)], input=json.dumps(["1.0.0", "01.0.0", "1.0.0-rc.1"]),
                                text=True, capture_output=True, check=True)
        values = json.loads(result.stdout)
        self.assertEqual(values[0], {"version": "1.0.0", "version_valid": True, "is_prerelease": False})
        self.assertEqual(values[1], {"version": "01.0.0", "version_valid": False, "is_prerelease": False})
        self.assertTrue(values[2]["is_prerelease"])

    def test_invalid_and_prerelease_consumers_get_exclusion_reasons(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root, extra_versions=True)
            run(root)
            output = root / "normalized/parquet"
            quality = pq.read_table(output / "version_quality.parquet").to_pylist()
            reasons = {r["version"]: r["version_exclusion_reason"] for r in quality if r["package_name"] == "@scope/app"}
            self.assertEqual(reasons, {"1.0.0": None, "01.0.0": "invalid_semver", "1.0.0-rc.1": "prerelease_version"})
            inputs = pq.read_table(output / "technical_lag_inputs.parquet").to_pylist()
            invalid = [r for r in inputs if r["source_version"] != "1.0.0"]
            self.assertEqual(len(invalid), 8)
            self.assertTrue(all(r["preparation_exclusion_reason"] == "invalid_consumer_version_or_time" for r in invalid))


if __name__ == "__main__":
    unittest.main()
