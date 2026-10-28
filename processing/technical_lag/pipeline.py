from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import shutil
import subprocess
import tempfile
from pathlib import Path, PurePosixPath

LOG = logging.getLogger(__name__)
KEYS = ("source_package", "source_version", "dependency_type", "dependency_name")
PROVENANCE = ("snapshot_id", "request_url", "retrieved_at", "raw_path", "raw_sha256", "parser_version")


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def safe_relative(value: str) -> Path:
    posix = PurePosixPath(value)
    if posix.is_absolute() or ".." in posix.parts or not posix.parts:
        raise ValueError(f"unsafe manifest path: {value}")
    return Path(*posix.parts)


def calculation_version() -> str:
    value = hashlib.sha256()
    root = Path(__file__).parent
    for path in (Path(__file__), root / "core.cjs", root / "runner.cjs", root / "package-lock.json"):
        value.update(path.read_bytes())
    return "technical-lag@1.0.0+source." + value.hexdigest()


def _sql_string(value: Path | str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _verify_files(root: Path, entries: list[dict], label: str) -> None:
    for entry in entries:
        path = root / safe_relative(entry["path"])
        if not path.is_file() or path.stat().st_size != entry["bytes"] or digest(path) != entry["sha256"]:
            raise ValueError(f"{label} SHA-256 mismatch: {entry['path']}")


def _parquet_schemas(pa):
    text = pa.string()
    provenance = pa.struct([(key, text) for key in PROVENANCE])
    calculated = pa.schema([
        *[(key, text) for key in KEYS],
        ("effective_dependency_type", text),
        ("status", text),
        ("exclusion_reason", text),
        ("v_req", text),
        ("v_latest", text),
        ("available_versions", pa.int64()),
        ("version_lag", pa.int64()),
        ("time_lag_days", pa.float64()),
        ("time_lag_nanoseconds", text),
        ("update_type", text),
        ("nonmonotone_publish_order", pa.bool_()),
    ])
    diagnostics = pa.schema([
        ("package_name", text),
        ("provenance", provenance),
        ("total_versions", pa.int64()),
        ("invalid_versions", pa.int64()),
        ("prerelease_versions", pa.int64()),
        ("stable_versions", pa.int64()),
        ("stable_versions_missing_time", pa.int64()),
    ])
    return calculated, diagnostics


def _jsonl_to_parquet(source: Path, destination: Path, schema, pa, pq, batch_size: int = 16384) -> int:
    count = 0
    batch = []
    with source.open(encoding="utf-8") as stream, pq.ParquetWriter(
            destination, schema, compression="zstd", version="2.6") as writer:
        for number, line in enumerate(stream, 1):
            try:
                batch.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid calculator JSON at line {number}") from exc
            if len(batch) >= batch_size:
                writer.write_table(pa.Table.from_pylist(batch, schema=schema))
                count += len(batch)
                batch.clear()
        if batch:
            writer.write_table(pa.Table.from_pylist(batch, schema=schema))
            count += len(batch)
    return count


def _write_histories(db, path: Path) -> tuple[int, int]:
    query = """
        SELECT p.package_name, p.provenance, v.version, v.published_at
        FROM packages p
        JOIN needed_dependencies n USING(package_name)
        LEFT JOIN versions v USING(package_name)
        ORDER BY p.package_name, v.version
    """
    reader = db.execute(query).fetch_record_batch(rows_per_batch=16384)
    package_count = version_count = 0
    current_name = None
    current_provenance = None
    releases = []

    def flush(stream):
        nonlocal package_count
        if current_name is not None:
            stream.write(json.dumps({"package_name": current_name, "provenance": current_provenance,
                                     "versions": releases}, ensure_ascii=False, separators=(",", ":")) + "\n")
            package_count += 1

    with path.open("w", encoding="utf-8") as stream:
        for batch in reader:
            for row in batch.to_pylist():
                if row["package_name"] != current_name:
                    flush(stream)
                    current_name = row["package_name"]
                    current_provenance = row["provenance"]
                    releases = []
                elif row["provenance"] != current_provenance:
                    raise ValueError(f"multiple npm snapshots for dependency history: {current_name}")
                if row["version"] is not None:
                    releases.append({"version": row["version"], "published_at": row["published_at"]})
                    version_count += 1
        flush(stream)
    return package_count, version_count


def _run_calculator(db, staging: Path, runner: Path) -> tuple[int, int, int]:
    histories_path = staging / ".histories.jsonl"
    diagnostics_json = staging / ".diagnostics.jsonl"
    results_json = staging / ".results.jsonl"
    stderr_path = staging / ".calculator.stderr"
    package_count, version_count = _write_histories(db, histories_path)
    query = """
        SELECT source_package, source_version, dependency_name, requirement, dependency_type,
               shadowed_by_optional, observation_time,
               consumer_provenance.retrieved_at AS consumer_retrieved_at,
               dependency_package_matched
        FROM lag_inputs
        WHERE consumer_version_valid AND NOT consumer_is_prerelease AND observation_time IS NOT NULL
          AND dependency_type='dependencies' AND NOT shadowed_by_optional
    """
    process = None
    evaluated = 0
    with results_json.open("wb") as output, stderr_path.open("wb") as errors:
        process = subprocess.Popen(
            ["node", str(runner), str(histories_path), str(diagnostics_json)],
            stdin=subprocess.PIPE, stdout=output, stderr=errors,
        )
        try:
            assert process.stdin is not None
            reader = db.execute(query).fetch_record_batch(rows_per_batch=16384)
            for batch in reader:
                for row in batch.to_pylist():
                    process.stdin.write((json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n").encode())
                    evaluated += 1
                if evaluated and evaluated % 100000 < batch.num_rows:
                    LOG.info("technical-lag rows sent to calculator: %s", evaluated)
            process.stdin.close()
            return_code = process.wait()
        except BaseException:
            if process.stdin and not process.stdin.closed:
                process.stdin.close()
            process.terminate()
            process.wait()
            raise
    if return_code:
        error = stderr_path.read_text(encoding="utf-8", errors="replace")[-8000:]
        raise RuntimeError(f"technical-lag calculator failed ({return_code}):\n{error}")
    progress = stderr_path.read_text(encoding="utf-8", errors="replace").strip()
    if progress:
        LOG.info("calculator progress:\n%s", progress)
    return evaluated, package_count, version_count


def _check(db, checks: dict[str, int], name: str, query: str) -> None:
    count = db.execute(query).fetchone()[0]
    checks[name] = count
    if count:
        raise ValueError(f"integrity check {name}: {count} violations")


def _build(staging: Path, input_dir: Path, version: str, duckdb, pa, pq) -> dict:
    db = duckdb.connect()
    # Full-v1 joins tens of millions of declarations with calculator output.
    # Keep enough buffer-pool headroom for the compressed COPY while retaining
    # an on-disk spill directory for larger-than-memory operators.
    db.execute("SET memory_limit='4GB'")
    db.execute("SET threads=2")
    db.execute("SET preserve_insertion_order=false")
    db.execute(f"SET temp_directory={_sql_string(staging / '.spill')}")
    # DuckDB derives this guard once, when the connection is opened.  The
    # calculator's transient JSONL files are removed before the large join, so
    # the later free space can legitimately exceed that initial estimate.
    db.execute("SET max_temp_directory_size='40GB'")
    checks: dict[str, int] = {}
    key_sql = ", ".join(KEYS)
    join_sql = " AND ".join(f"i.{key}=c.{key}" for key in KEYS)
    candidate_filter = """consumer_version_valid AND NOT consumer_is_prerelease AND observation_time IS NOT NULL
        AND dependency_type='dependencies' AND NOT shadowed_by_optional"""
    candidate_filter_i = """i.consumer_version_valid AND NOT i.consumer_is_prerelease AND i.observation_time IS NOT NULL
        AND i.dependency_type='dependencies' AND NOT i.shadowed_by_optional"""
    try:
        db.execute(f"CREATE VIEW packages AS SELECT * FROM read_parquet({_sql_string(input_dir / 'packages/*.parquet')})")
        db.execute(f"CREATE VIEW versions AS SELECT * FROM read_parquet({_sql_string(input_dir / 'versions/*.parquet')})")
        db.execute(f"CREATE VIEW lag_inputs AS SELECT * FROM read_parquet({_sql_string(input_dir / 'technical_lag_inputs.parquet')})")
        _check(db, checks, "input_duplicate_keys",
               f"SELECT count(*) FROM (SELECT {key_sql} FROM lag_inputs GROUP BY ALL HAVING count(*)>1)")
        _check(db, checks, "input_null_keys",
               "SELECT count(*) FROM lag_inputs WHERE " + " OR ".join(f"{key} IS NULL" for key in KEYS))
        _check(db, checks, "input_invalid_flags", """SELECT count(*) FROM lag_inputs WHERE
            consumer_version_valid IS NULL OR consumer_is_prerelease IS NULL OR shadowed_by_optional IS NULL
            OR dependency_package_matched IS NULL OR historical_mode IS DISTINCT FROM 'reconstructed_survivors'""")
        db.execute(f"""CREATE TEMP TABLE needed_dependencies AS SELECT DISTINCT dependency_name AS package_name
            FROM lag_inputs WHERE {candidate_filter} AND dependency_package_matched""")
        _check(db, checks, "matched_dependencies_without_package", """SELECT count(*) FROM needed_dependencies n
            ANTI JOIN packages p USING(package_name)""")
        evaluated, history_packages, history_versions = _run_calculator(
            db, staging, Path(__file__).with_name("runner.cjs"))
        calculated_schema, diagnostics_schema = _parquet_schemas(pa)
        calculated_rows = _jsonl_to_parquet(staging / ".results.jsonl", staging / ".calculated.parquet",
                                             calculated_schema, pa, pq)
        diagnostics_rows = _jsonl_to_parquet(staging / ".diagnostics.jsonl",
                                              staging / "dependency_history_diagnostics.parquet",
                                              diagnostics_schema, pa, pq)
        if calculated_rows != evaluated:
            raise ValueError(f"calculator row accounting mismatch: sent {evaluated}, received {calculated_rows}")
        if diagnostics_rows != history_packages:
            raise ValueError(f"history diagnostics mismatch: {history_packages} histories, {diagnostics_rows} rows")
        # The JSONL exchange is no longer needed once both Parquet files have
        # been closed.  Releasing it here avoids carrying several gigabytes of
        # dead staging data through the much larger final join.
        for path in (staging / ".histories.jsonl", staging / ".diagnostics.jsonl",
                     staging / ".results.jsonl", staging / ".calculator.stderr"):
            path.unlink(missing_ok=True)
        db.execute(f"CREATE VIEW calculated AS SELECT * FROM read_parquet({_sql_string(staging / '.calculated.parquet')})")
        expected_evaluated = db.execute(f"SELECT count(*) FROM lag_inputs WHERE {candidate_filter}").fetchone()[0]
        if evaluated != expected_evaluated:
            raise ValueError(f"calculator input accounting mismatch: expected {expected_evaluated}, sent {evaluated}")
        _check(db, checks, "calculated_duplicate_keys",
               f"SELECT count(*) FROM (SELECT {key_sql} FROM calculated GROUP BY ALL HAVING count(*)>1)")
        _check(db, checks, "calculated_orphans", f"""SELECT count(*) FROM calculated c ANTI JOIN lag_inputs i
            ON {join_sql}""")
        _check(db, checks, "calculator_missing_rows", f"""SELECT count(*) FROM lag_inputs i LEFT JOIN calculated c
            ON {join_sql} WHERE {candidate_filter_i}
            AND c.source_package IS NULL""")
        calculation = _sql_string(version)
        output_path = staging / "technical_lag.parquet"
        db.execute(f"""COPY (SELECT i.*, {calculation} AS calculation_version,
            CASE WHEN i.shadowed_by_optional THEN 'optionalDependencies' ELSE i.dependency_type END AS effective_dependency_type,
            CASE WHEN NOT ({candidate_filter_i}) THEN 'excluded' ELSE c.status END AS status,
            CASE WHEN NOT i.consumer_version_valid OR i.consumer_is_prerelease OR i.observation_time IS NULL
                    THEN 'invalid_consumer_version_or_time'
                 WHEN i.dependency_type <> 'dependencies' OR i.shadowed_by_optional
                    THEN 'unsupported_dependency_type'
                 ELSE c.exclusion_reason END AS exclusion_reason,
            c.v_req, c.v_latest, c.available_versions, c.version_lag, c.time_lag_days,
            c.time_lag_nanoseconds, c.update_type, c.nonmonotone_publish_order
            FROM lag_inputs i LEFT JOIN calculated c ON {join_sql})
            TO {_sql_string(output_path)} (FORMAT PARQUET, COMPRESSION ZSTD)""")
        db.execute(f"CREATE VIEW output AS SELECT * FROM read_parquet({_sql_string(output_path)})")
        _check(db, checks, "output_cardinality",
               "SELECT abs((SELECT count(*) FROM lag_inputs) - (SELECT count(*) FROM output))")
        _check(db, checks, "output_duplicate_keys",
               f"SELECT count(*) FROM (SELECT {key_sql} FROM output GROUP BY ALL HAVING count(*)>1)")
        _check(db, checks, "invalid_status_reason", """SELECT count(*) FROM output WHERE
            (status='ok' AND exclusion_reason IS NOT NULL) OR
            (status='excluded' AND exclusion_reason IS NULL) OR status NOT IN ('ok','excluded')""")
        metrics = "v_req, v_latest, available_versions, version_lag, time_lag_days, time_lag_nanoseconds, update_type, nonmonotone_publish_order"
        _check(db, checks, "successful_missing_metrics",
               "SELECT count(*) FROM output WHERE status='ok' AND (" + " OR ".join(f"{field} IS NULL" for field in metrics.split(", ")) + ")")
        _check(db, checks, "excluded_nonnull_metrics",
               "SELECT count(*) FROM output WHERE status='excluded' AND (" + " OR ".join(f"{field} IS NOT NULL" for field in metrics.split(", ")) + ")")
        _check(db, checks, "invalid_metric_values", """SELECT count(*) FROM output WHERE status='ok' AND
            (version_lag<0 OR available_versions<version_lag+1 OR update_type NOT IN ('none','patch','minor','major'))""")
        total, successful = db.execute("SELECT count(*), count(*) FILTER (WHERE status='ok') FROM output").fetchone()
        exclusion_reasons = dict(db.execute("""SELECT exclusion_reason, count(*) FROM output
            WHERE status='excluded' GROUP BY 1 ORDER BY 1""").fetchall())
        update_types = dict(db.execute("""SELECT update_type, count(*) FROM output
            WHERE status='ok' GROUP BY 1 ORDER BY 1""").fetchall())
        nonmonotone = db.execute("""SELECT count(*) FROM output
            WHERE status='ok' AND nonmonotone_publish_order""").fetchone()[0]
        diagnostic_names = ("total_versions", "invalid_versions", "prerelease_versions",
                            "stable_versions", "stable_versions_missing_time")
        diagnostic_values = db.execute(f"""SELECT
            sum(total_versions), sum(invalid_versions), sum(prerelease_versions), sum(stable_versions),
            sum(stable_versions_missing_time) FROM read_parquet({_sql_string(staging / 'dependency_history_diagnostics.parquet')})""").fetchone()
        diagnostics = dict(zip(diagnostic_names, diagnostic_values))
        return {
            "rows": {"total": total, "successful": successful, "unsuccessful": total - successful,
                     "success_fraction": successful / total if total else 0.0,
                     "evaluated_by_semver_core": evaluated},
            "exclusion_reasons": exclusion_reasons,
            "update_types": update_types,
            "nonmonotone_publish_order": nonmonotone,
            "dependency_histories": {"packages": history_packages, "versions": history_versions,
                                     **{key: int(value or 0) for key, value in diagnostics.items()}},
            "integrity_violations": checks,
        }
    finally:
        db.close()
        for path in (staging / ".histories.jsonl", staging / ".diagnostics.jsonl",
                     staging / ".results.jsonl", staging / ".calculator.stderr",
                     staging / ".calculated.parquet"):
            path.unlink(missing_ok=True)
        shutil.rmtree(staging / ".spill", ignore_errors=True)


def render_report(report: dict) -> str:
    rows = report["rows"]
    lines = [
        "# Pilot technical lag",
        "",
        "Calculated for every normalized dependency requirement; unsuitable observations retain NULL metrics and an exclusion reason.",
        "",
        "| Result | Rows |",
        "| --- | ---: |",
        f"| total | {rows['total']:,} |",
        f"| successful | {rows['successful']:,} |",
        f"| unsuccessful | {rows['unsuccessful']:,} |",
        f"| evaluated by SemVer core | {rows['evaluated_by_semver_core']:,} |",
        "",
        f"Success rate: {rows['success_fraction']:.4%}.",
        "",
        "## Exclusions",
        "",
        "| Reason | Rows |",
        "| --- | ---: |",
    ]
    lines.extend(f"| {reason} | {count:,} |" for reason, count in report["exclusion_reasons"].items())
    lines += ["", "## Available update type", "", "| Type | Rows |", "| --- | ---: |"]
    lines.extend(f"| {kind} | {count:,} |" for kind, count in report["update_types"].items())
    histories = report["dependency_histories"]
    lines += [
        "",
        f"Non-monotone publication order: {report['nonmonotone_publish_order']:,} successful rows.",
        f"Prepared dependency histories: {histories['packages']:,} packages / {histories['versions']:,} versions.",
        f"Integrity checks: {len(report['integrity_violations'])}, all zero violations.",
        "",
        "The unit is an npm manifest declaration (`dependency_requirements`), because deps.dev graph edges do not contain the declared constraint.",
        "deps.dev `dependency_relations` remain a separate observed resolution graph and are not substituted for historical `v_req`.",
        "Historical mode is `reconstructed_survivors`; timestamps preserve the normalized RFC3339 precision.",
        "Build identity and output checksums are stored in technical-lag-manifest.json.",
        "",
    ]
    return "\n".join(lines)


def run(input_dir: Path, output_dir: Path | None = None) -> dict:
    import duckdb
    import pyarrow as pa
    import pyarrow.parquet as pq

    input_dir = input_dir.resolve()
    destination = (output_dir if output_dir is not None else
                   input_dir.parent.parent / "pilot-v1-technical-lag" / "technical_lag").resolve()
    if destination == input_dir or input_dir in destination.parents or destination in input_dir.parents:
        raise ValueError("output must be separate from the immutable normalized dataset")
    manifest_path = input_dir / "normalization-manifest.json"
    if not manifest_path.is_file():
        raise ValueError(f"normalization manifest not found: {manifest_path}")
    normalized_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    _verify_files(input_dir, normalized_manifest["outputs"], "normalized input")
    expected_rows = normalized_manifest["report"]["counts"]["dependency_requirements"]
    semver_version = subprocess.run(
        ["node", "-p", "require('./node_modules/semver/package.json').version"],
        cwd=Path(__file__).parent, capture_output=True, text=True, check=True).stdout.strip()
    if semver_version != "7.8.4":
        raise ValueError("technical lag requires semver@7.8.4; run npm ci --prefix processing/technical_lag")
    node_version = subprocess.run(["node", "--version"], capture_output=True, text=True, check=True).stdout.strip()
    version = calculation_version()
    identity = {
        "input_normalization_manifest_sha256": digest(manifest_path),
        "calculation_version": version,
        "dependencies": {"python": platform.python_version(), "node": node_version,
                         "pyarrow": pa.__version__, "duckdb": duckdb.__version__, "semver": semver_version},
    }
    if destination.exists():
        result_path = destination / "technical-lag-manifest.json"
        if not result_path.is_file():
            raise ValueError("existing technical-lag output has no manifest; use a separate output path")
        previous = json.loads(result_path.read_text(encoding="utf-8"))
        if previous["build"] != identity:
            raise ValueError("existing technical-lag build has different inputs/calculator/runtime; use a separate output path")
        _verify_files(destination, previous["outputs"], "technical-lag output")
        return previous["report"]
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".technical-lag-", dir=destination.parent))
    try:
        LOG.info("Calculating technical lag from %s", input_dir)
        report = _build(staging, input_dir, version, duckdb, pa, pq)
        if report["rows"]["total"] != expected_rows:
            raise ValueError(f"normalized row accounting mismatch: expected {expected_rows}, got {report['rows']['total']}")
        (staging / "report.md").write_text(render_report(report), encoding="utf-8")
        outputs = [{"path": path.relative_to(staging).as_posix(), "sha256": digest(path), "bytes": path.stat().st_size}
                   for path in sorted(staging.rglob("*")) if path.is_file()]
        result = {"build": identity, "report": report, "outputs": outputs}
        (staging / "technical-lag-manifest.json").write_text(
            json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        os.rename(staging, destination)
        return report
    finally:
        if staging.exists():
            shutil.rmtree(staging)
