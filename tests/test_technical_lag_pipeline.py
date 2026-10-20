import json
import tempfile
import unittest
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from processing.normalization.pipeline import schemas
from processing.technical_lag.pipeline import digest, run


PROVENANCE_FIELDS = ("snapshot_id", "request_url", "retrieved_at", "raw_path", "raw_sha256", "parser_version")


class TechnicalLagPipelineTest(unittest.TestCase):
    def fixture(self, root: Path) -> Path:
        normalized = root / "normalized"
        (normalized / "packages").mkdir(parents=True)
        (normalized / "versions").mkdir()
        provenance_type = pa.struct([(key, pa.string()) for key in PROVENANCE_FIELDS])
        base = {
            "snapshot_id": "f14b28f7-663d-4bb5-b191-12e2b4215bd0",
            "request_url": "https://registry.npmjs.org/app",
            "retrieved_at": "2026-04-01T00:00:00Z",
            "raw_path": "raw/npm/app.json",
            "raw_sha256": "a" * 64,
            "parser_version": "fixture@1",
        }
        dependency_provenance = {**base, "request_url": "https://registry.npmjs.org/lib",
                                 "raw_path": "raw/npm/lib.json", "raw_sha256": "b" * 64}
        common = {"schema_version": "1.0.0", "source": "npm"}
        package_rows = [
            {**common, "provenance": base, "package_name": "app", "repository": None,
             "dist_tags": "{}", "created_at": None, "modified_at": None},
            {**common, "provenance": dependency_provenance, "package_name": "lib", "repository": None,
             "dist_tags": "{}", "created_at": None, "modified_at": None},
        ]
        version_rows = [
            {**common, "provenance": base, "package_name": "app", "version": "1.0.0",
             "published_at": "2026-03-01T00:00:00Z", "repository": None,
             "is_deprecated": False, "deprecated_message": None},
            {**common, "provenance": dependency_provenance, "package_name": "lib", "version": "1.0.0",
             "published_at": "2026-01-01T00:00:00Z", "repository": None,
             "is_deprecated": False, "deprecated_message": None},
            {**common, "provenance": dependency_provenance, "package_name": "lib", "version": "1.1.0",
             "published_at": "2026-01-11T00:00:00Z", "repository": None,
             "is_deprecated": False, "deprecated_message": None},
        ]
        contract_schemas = schemas(pa)
        pq.write_table(pa.Table.from_pylist(package_rows, schema=contract_schemas["packages"]),
                       normalized / "packages/part-00000.parquet")
        pq.write_table(pa.Table.from_pylist(version_rows, schema=contract_schemas["versions"]),
                       normalized / "versions/part-00000.parquet")

        input_schema = pa.schema([
            ("schema_version", pa.string()), ("source", pa.string()), ("provenance", provenance_type),
            ("source_package", pa.string()), ("source_version", pa.string()),
            ("dependency_name", pa.string()), ("requirement", pa.string()), ("dependency_type", pa.string()),
            ("observation_time", pa.string()), ("consumer_provenance", provenance_type),
            ("dependency_provenance", provenance_type), ("dependency_package_matched", pa.bool_()),
            ("consumer_version_valid", pa.bool_()), ("consumer_is_prerelease", pa.bool_()),
            ("shadowed_by_optional", pa.bool_()), ("preparation_exclusion_reason", pa.string()),
            ("historical_mode", pa.string()),
        ])

        def lag_row(name, requirement, dependency_type="dependencies", matched=False, shadowed=False,
                    preparation=None):
            return {**common, "provenance": base, "source_package": "app", "source_version": "1.0.0",
                    "dependency_name": name, "requirement": requirement, "dependency_type": dependency_type,
                    "observation_time": "2026-03-01T00:00:00Z", "consumer_provenance": base,
                    "dependency_provenance": dependency_provenance if matched else None,
                    "dependency_package_matched": matched, "consumer_version_valid": True,
                    "consumer_is_prerelease": False, "shadowed_by_optional": shadowed,
                    "preparation_exclusion_reason": preparation,
                    "historical_mode": "reconstructed_survivors"}

        input_rows = [
            lag_row("lib", "1.0.0", matched=True),
            lag_row("lib", "*", dependency_type="devDependencies", matched=True,
                    preparation="unsupported_dependency_type"),
            lag_row("missing-tag", "latest", preparation="missing_dependency_snapshot"),
            lag_row("missing", "^1.0.0", preparation="missing_dependency_snapshot"),
            lag_row("optional-lib", "^1.0.0", shadowed=True, preparation="optional_dependency_override"),
        ]
        pq.write_table(pa.Table.from_pylist(input_rows, schema=input_schema),
                       normalized / "technical_lag_inputs.parquet")
        outputs = []
        for path in sorted(normalized.rglob("*.parquet")):
            outputs.append({"path": path.relative_to(normalized).as_posix(), "sha256": digest(path),
                            "bytes": path.stat().st_size})
        manifest = {"build": {"fixture": True},
                    "report": {"counts": {"dependency_requirements": len(input_rows)}},
                    "outputs": outputs}
        (normalized / "normalization-manifest.json").write_text(
            json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8")
        return normalized

    def test_pipeline_calculates_all_rows_and_reuses_verified_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self.fixture(root)
            destination = root / "technical-lag"
            report = run(source, destination)
            self.assertEqual(report["rows"]["total"], 5)
            self.assertEqual(report["rows"]["successful"], 1)
            self.assertEqual(report["rows"]["unsuccessful"], 4)
            self.assertEqual(report["rows"]["evaluated_by_semver_core"], 3)
            self.assertEqual(report["exclusion_reasons"], {
                "missing_dependency_snapshot": 1,
                "unsupported_dependency_type": 2,
                "unsupported_requirement": 1,
            })
            rows = {(row["dependency_type"], row["dependency_name"]): row
                    for row in pq.read_table(destination / "technical_lag.parquet").to_pylist()}
            success = rows[("dependencies", "lib")]
            self.assertEqual((success["v_req"], success["v_latest"]), ("1.0.0", "1.1.0"))
            self.assertEqual(success["version_lag"], 1)
            self.assertEqual(success["time_lag_days"], 10)
            self.assertEqual(success["update_type"], "minor")
            self.assertEqual(rows[("dependencies", "missing-tag")]["exclusion_reason"],
                             "unsupported_requirement")
            self.assertEqual(run(source, destination), report)

    def test_existing_corrupt_output_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self.fixture(root)
            destination = root / "technical-lag"
            run(source, destination)
            with (destination / "report.md").open("a", encoding="utf-8") as stream:
                stream.write("corrupt")
            with self.assertRaisesRegex(ValueError, "output SHA-256 mismatch"):
                run(source, destination)

    def test_output_cannot_overlap_normalized_input(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = self.fixture(Path(temporary))
            with self.assertRaisesRegex(ValueError, "output must be separate"):
                run(source, source / "technical-lag")


if __name__ == "__main__":
    unittest.main()
