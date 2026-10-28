from __future__ import annotations

import hashlib
import gzip
import json
import logging
import os
import shutil
import subprocess
import tarfile
import tempfile
import uuid
from collections import Counter
from pathlib import Path, PurePosixPath

from collector.npm import collector as npm
from collector.depsdev import collector as depsdev

LOG = logging.getLogger(__name__)
PROVENANCE = ("snapshot_id", "request_url", "retrieved_at", "raw_path", "raw_sha256", "parser_version")


def digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def parser_version():
    # Includes the reused parsers, schemas and join implementation, not the Git worktree state.
    value = hashlib.sha256()
    for path in (Path(__file__), Path(npm.__file__), Path(depsdev.__file__),
                 Path(__file__).with_name("version-quality.cjs"), Path(__file__).with_name("package-lock.json")):
        value.update(path.read_bytes())
    return "pilot-normalization@1.0.0+source." + value.hexdigest()


def safe_path(value):
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValueError(f"unsafe archive path: {value}")
    return str(path)


def archive_records(dataset, shards):
    for shard in shards:
        path = dataset / safe_path(shard["path"])
        if digest(path) != shard["sha256"]:
            raise ValueError(f"archive SHA-256 mismatch: {path}")
        count = 0
        members = {}
        with tarfile.open(path, "r|gz") as archive:
            for member in archive:
                safe_path(member.name)
                if not member.isfile():
                    raise ValueError(f"unexpected non-file archive member: {member.name}")
                with archive.extractfile(member) as stream:
                    payload = stream.read()
                payload_hash = hashlib.sha256(payload).digest()
                previous = members.get(member.name)
                if previous is not None:
                    if previous != payload_hash:
                        raise ValueError(f"conflicting duplicate archive member: {path}:{member.name}")
                    continue
                members[member.name] = payload_hash
                yield member.name, payload
                count += 1
        if count != shard["files"]:
            raise ValueError(f"archive member count mismatch: {path}")


def states(dataset, manifest, source):
    result = {}
    for name, payload in archive_records(dataset, manifest["shards"]["audit"][source]):
        if not name.startswith(f"state/{source}/"):
            continue
        state = json.loads(payload)
        path = safe_path(state["raw_path"])
        if path in result:
            raise ValueError(f"duplicate state for {path}")
        if state.get("http_status") != 200:
            raise ValueError(f"non-successful state: {name}")
        result[path] = state
    if not result and not manifest["shards"]["raw"].get(source) and not manifest["shards"]["audit"].get(source):
        return result
    if not result:
        raise ValueError(f"no successful {source} states")
    if source == "depsdev" and any("retrieved_at" not in state for state in result.values()):
        # Legacy state markers omitted retrieval time. Recover only the archived
        # provenance, never relation fields or a timestamp guessed from file mtime.
        for shard in manifest["shards"].get("normalized", {}).get("dependency_relation", []):
            path = dataset / safe_path(shard["path"])
            if digest(path) != shard["sha256"]:
                raise ValueError(f"provenance shard SHA-256 mismatch: {path}")
            rows = 0
            with gzip.open(path, "rt", encoding="utf-8") as stream:
                for line in stream:
                    provenance = json.loads(line)["provenance"]
                    state = result.get(provenance["raw_path"])
                    if state is None or any(state[key] != provenance[key] for key in PROVENANCE if key != "retrieved_at"):
                        raise ValueError("legacy provenance disagrees with audit state")
                    if state.get("retrieved_at", provenance["retrieved_at"]) != provenance["retrieved_at"]:
                        raise ValueError("inconsistent legacy retrieval timestamps")
                    state["retrieved_at"] = provenance["retrieved_at"]
                    rows += 1
            if rows != shard["rows"]:
                raise ValueError(f"provenance shard row count mismatch: {path}")
    return result


def raw_records(dataset, manifest, source, version):
    remaining = states(dataset, manifest, source)
    for path, payload in archive_records(dataset, manifest["shards"]["raw"][source]):
        # npm discovery responses are archived too; only active packuments have state markers.
        if source == "npm" and path.startswith(("raw/npm-search/", "raw/npm-registry-index/")):
            continue
        state = remaining.pop(path, None)
        if state is None:
            raise ValueError(f"raw response has no unique active state: {path}")
        if hashlib.sha256(payload).hexdigest() != state["raw_sha256"]:
            raise ValueError(f"raw SHA-256 mismatch: {path}")
        provenance = {key: state.get(key) for key in PROVENANCE}
        uuid.UUID(provenance["snapshot_id"])
        if any(not isinstance(v, str) or not v for key, v in provenance.items() if key != "retrieved_at"):
            raise ValueError(f"invalid provenance: {path}")
        timestamp = provenance["retrieved_at"]
        if timestamp is None and source != "depsdev":
            raise ValueError(f"missing retrieved_at: {path}")
        if timestamp is not None and (not isinstance(timestamp, str) or npm.normalize_timestamp(timestamp) != timestamp):
            raise ValueError(f"invalid UTC retrieved_at: {path}")
        provenance["parser_version"] = version
        yield json.loads(payload), provenance, state
    if remaining:
        raise ValueError(f"{source}: {len(remaining)} active raw responses missing")


def schemas(pa):
    text = pa.string()
    common = [("schema_version", text), ("source", text),
              ("provenance", pa.struct([(key, text) for key in PROVENANCE]))]
    definitions = {
        "packages": [("package_name", text), ("repository", text), ("dist_tags", text),
                     ("created_at", text), ("modified_at", text)],
        "versions": [("package_name", text), ("version", text), ("published_at", text),
                     ("repository", text), ("is_deprecated", pa.bool_()), ("deprecated_message", text)],
        "dependency_requirements": [(key, text) for key in
                                    ("source_package", "source_version", "dependency_name", "requirement", "dependency_type")],
        "dependency_relations": [(key, text) for key in
                                 ("root_package", "root_version", "source_package", "source_version",
                                  "dependency_package", "resolved_version")] +
                                [(key, pa.int64()) for key in ("edge_index", "from_node", "to_node", "depth")],
    }
    return {name: pa.schema(common + fields, metadata={
        b"data_contract": b"1.0.0",
        b"json_columns": ",".join(key for key, _ in fields if key in {"repository", "dist_tags"}).encode(),
        b"timestamp_encoding": b"RFC3339 UTC string; original precision preserved",
    }) for name, fields in definitions.items()}


class Writer:
    def __init__(self, directory, schema, pa, pq, batch_size=16384):
        directory.mkdir()
        self.path = directory / "part-00000.parquet"
        self.schema, self.pa, self.batch_size = schema, pa, batch_size
        self.writer = pq.ParquetWriter(self.path, schema, compression="zstd", version="2.6")
        self.buffer, self.rows = [], 0

    def add(self, record):
        record = dict(record)
        for key in ("repository", "dist_tags"):
            if key in record and record[key] is not None:
                record[key] = json.dumps(record[key], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        self.buffer.append(record)
        if len(self.buffer) >= self.batch_size:
            self.flush()

    def flush(self):
        if self.buffer:
            self.writer.write_table(self.pa.Table.from_pylist(self.buffer, schema=self.schema))
            self.rows += len(self.buffer)
            self.buffer.clear()

    def close(self):
        self.flush()
        self.writer.close()


def sql_string(value):
    return "'" + str(value).replace("'", "''") + "'"


def analyze(directory, duckdb):
    db = duckdb.connect()
    db.execute("SET memory_limit='1GB'")
    db.execute("SET threads=4")
    db.execute(f"SET temp_directory={sql_string(directory / 'spill')}")
    checks = {}

    def check(name, query):
        count = db.execute(query).fetchone()[0]
        checks[name] = count
        if count:
            raise ValueError(f"integrity check {name}: {count} violations")

    try:
        for name in ("packages", "versions", "dependency_requirements", "dependency_relations"):
            db.execute(f"CREATE VIEW {name} AS SELECT * FROM read_parquet({sql_string(directory / name / '*.parquet')})")
        keys = {
            "packages": "package_name",
            "versions": "package_name, version",
            "dependency_requirements": "source_package, source_version, dependency_type, dependency_name",
            "dependency_relations": "root_package, root_version, provenance.snapshot_id, provenance.parser_version, edge_index",
        }
        for name, key in keys.items():
            check(name + "_duplicate_keys", f"SELECT count(*) FROM (SELECT {key} FROM {name} GROUP BY ALL HAVING count(*)>1)")
            source = "depsdev" if name == "dependency_relations" else "npm"
            check(name + "_invalid_common", f"""SELECT count(*) FROM {name} WHERE
                schema_version IS DISTINCT FROM '1.0.0' OR source IS DISTINCT FROM '{source}'
                OR try_cast(provenance.snapshot_id AS UUID) IS NULL
                OR provenance.parser_version IS NULL OR provenance.parser_version=''
                OR provenance.request_url IS NULL OR provenance.request_url=''
                OR provenance.raw_path IS NULL OR provenance.raw_path=''
                OR provenance.retrieved_at IS NULL OR try_cast(provenance.retrieved_at AS TIMESTAMPTZ) IS NULL
                OR provenance.raw_sha256 IS NULL
                OR NOT regexp_full_match(provenance.raw_sha256, '[0-9a-f]{{64}}')""")
            nulls = " OR ".join(f"{field.strip()} IS NULL" for field in key.split(","))
            check(name + "_null_keys", f"SELECT count(*) FROM {name} WHERE {nulls}")
        check("versions_without_package", """SELECT count(*) FROM versions v ANTI JOIN packages p USING(package_name)""")
        check("requirements_without_source_version", """SELECT count(*) FROM dependency_requirements r
              ANTI JOIN versions v ON r.source_package=v.package_name AND r.source_version=v.version""")
        check("invalid_requirement_type", """SELECT count(*) FROM dependency_requirements WHERE requirement IS NULL
              OR dependency_type NOT IN ('dependencies','devDependencies','peerDependencies','optionalDependencies')""")
        check("invalid_relation_indices", """SELECT count(*) FROM dependency_relations WHERE
              depth IS NULL OR depth<1 OR edge_index<0 OR from_node IS NULL OR from_node<0 OR to_node IS NULL OR to_node<0""")
        check("null_required_values", """SELECT
            (SELECT count(*) FROM packages WHERE dist_tags IS NULL) +
            (SELECT count(*) FROM versions WHERE is_deprecated IS NULL) +
            (SELECT count(*) FROM dependency_relations WHERE source_package IS NULL OR source_version IS NULL
                OR dependency_package IS NULL OR resolved_version IS NULL)""")
        check("relations_without_root_version", """SELECT count(*) FROM dependency_relations r ANTI JOIN versions v
              ON r.root_package=v.package_name AND r.root_version=v.version""")
        import pyarrow as pa
        distinct_versions = [row[0] for row in db.execute("SELECT DISTINCT version FROM versions ORDER BY version").fetchall()]
        quality = subprocess.run(["node", str(Path(__file__).with_name("version-quality.cjs"))],
                                 input=json.dumps(distinct_versions), text=True, capture_output=True, check=True)
        flags = json.loads(quality.stdout)
        db.register("version_flags", pa.Table.from_pylist(flags, schema=pa.schema([
            ("version", pa.string()), ("version_valid", pa.bool_()), ("is_prerelease", pa.bool_())])))
        quality_path = directory / "version_quality.parquet"
        db.execute(f"""COPY (SELECT v.*, f.version_valid, f.is_prerelease,
            CASE WHEN NOT f.version_valid THEN 'invalid_semver'
                 WHEN f.is_prerelease THEN 'prerelease_version'
                 WHEN v.published_at IS NULL THEN 'missing_publication_time' ELSE NULL END AS version_exclusion_reason
            FROM versions v JOIN version_flags f USING(version))
            TO {sql_string(quality_path)} (FORMAT PARQUET, COMPRESSION ZSTD)""")
        db.execute(f"CREATE VIEW version_quality AS SELECT * FROM read_parquet({sql_string(quality_path)})")
        check("version_quality_cardinality", "SELECT abs((SELECT count(*) FROM versions) - (SELECT count(*) FROM version_quality))")
        # All unmatched observations remain in the contract tables and in this explicit left join.
        matching = directory / "relation_matches.parquet"
        db.execute(f"""COPY (SELECT r.*,
            s.package_name IS NOT NULL AS source_version_matched,
            t.package_name IS NOT NULL AS resolved_version_matched,
            CASE WHEN s.package_name IS NULL AND t.package_name IS NULL THEN 'missing_source_and_target_versions'
                 WHEN s.package_name IS NULL THEN 'missing_source_version'
                 WHEN t.package_name IS NULL THEN 'missing_target_version' ELSE NULL END AS unmatched_reason
            FROM dependency_relations r
            LEFT JOIN versions s ON r.source_package=s.package_name AND r.source_version=s.version
            LEFT JOIN versions t ON r.dependency_package=t.package_name AND r.resolved_version=t.version)
            TO {sql_string(matching)} (FORMAT PARQUET, COMPRESSION ZSTD)""")
        db.execute(f"CREATE VIEW relation_matches AS SELECT * FROM read_parquet({sql_string(matching)})")
        total, missing_source, missing_target, unmatched = db.execute("""SELECT count(*),
            count(*) FILTER (WHERE NOT source_version_matched),
            count(*) FILTER (WHERE NOT resolved_version_matched),
            count(*) FILTER (WHERE unmatched_reason IS NOT NULL) FROM relation_matches""").fetchone()
        reasons = dict(db.execute("SELECT unmatched_reason, count(*) FROM relation_matches WHERE unmatched_reason IS NOT NULL GROUP BY 1 ORDER BY 1").fetchall())
        # One row per declaration. Alias names are deliberately not rewritten to a guessed target.
        inputs = directory / "technical_lag_inputs.parquet"
        db.execute(f"""COPY (SELECT r.*, v.published_at AS observation_time,
            v.provenance AS consumer_provenance, p.provenance AS dependency_provenance,
            p.package_name IS NOT NULL AS dependency_package_matched,
            v.version_valid AS consumer_version_valid, v.is_prerelease AS consumer_is_prerelease,
            r.dependency_type='dependencies' AND o.dependency_name IS NOT NULL AS shadowed_by_optional,
            CASE WHEN v.version_exclusion_reason IS NOT NULL THEN 'invalid_consumer_version_or_time'
                 WHEN r.dependency_type <> 'dependencies' THEN 'unsupported_dependency_type'
                 WHEN o.dependency_name IS NOT NULL THEN 'optional_dependency_override'
                 WHEN starts_with(trim(r.requirement), 'npm:') THEN 'unsupported_requirement'
                 WHEN v.published_at IS NULL THEN 'missing_consumer_publication_time'
                 WHEN p.package_name IS NULL THEN 'missing_dependency_snapshot' ELSE NULL END AS preparation_exclusion_reason,
            'reconstructed_survivors' AS historical_mode
            FROM dependency_requirements r
            JOIN version_quality v ON r.source_package=v.package_name AND r.source_version=v.version
            LEFT JOIN packages p ON r.dependency_name=p.package_name
            LEFT JOIN dependency_requirements o ON r.source_package=o.source_package AND r.source_version=o.source_version
                 AND r.dependency_name=o.dependency_name
                 AND o.dependency_type='optionalDependencies')
            TO {sql_string(inputs)} (FORMAT PARQUET, COMPRESSION ZSTD)""")
        db.execute(f"CREATE VIEW lag_inputs AS SELECT * FROM read_parquet({sql_string(inputs)})")
        req_total, req_unmatched = db.execute("SELECT count(*), count(*) FILTER (WHERE NOT dependency_package_matched) FROM lag_inputs").fetchone()
        eligible = db.execute("SELECT count(*) FROM lag_inputs WHERE preparation_exclusion_reason IS NULL").fetchone()[0]
        check("lag_input_cardinality", f"SELECT abs(count(*) - {req_total}) FROM dependency_requirements")
        counts = {name: db.execute(f"SELECT count(*) FROM {name}").fetchone()[0] for name in keys}
        return {
            "counts": counts, "integrity_violations": checks,
            "matching": {
                "relations": {"total": total, "unmatched": unmatched, "unmatched_fraction": unmatched / total if total else 0.0,
                              "missing_source_versions": missing_source, "missing_resolved_versions": missing_target,
                              "reasons": reasons},
                "requirement_dependency_packages": {"total": req_total, "unmatched": req_unmatched,
                    "unmatched_fraction": req_unmatched / req_total if req_total else 0.0,
                    "note": "exact declaration name lookup; npm aliases are not resolved"},
            },
            "technical_lag": {"prepared_candidates": eligible,
                "preparation_exclusions": dict(db.execute("SELECT preparation_exclusion_reason, count(*) FROM lag_inputs WHERE preparation_exclusion_reason IS NOT NULL GROUP BY 1 ORDER BY 1").fetchall()),
                "note": "Candidates still require semver@7.8.4 and historical eligibility checks; no lag metrics calculated."},
            "versions_missing_publication_time": db.execute("SELECT count(*) FROM versions WHERE published_at IS NULL").fetchone()[0],
            "version_quality": dict(db.execute("SELECT coalesce(version_exclusion_reason, 'stable_dated_version'), count(*) FROM version_quality GROUP BY 1 ORDER BY 1").fetchall()),
        }
    finally:
        db.close()
        spill = directory / "spill"
        if spill.exists():
            shutil.rmtree(spill)


def render_report(report):
    counts = report["counts"]
    relations = report["matching"]["relations"]
    requirements = report["matching"]["requirement_dependency_packages"]
    lines = ["# Pilot normalization", "", "Built from raw npm/deps.dev archives and archived audit states; Data Contract 1.0.0.", "",
             "| Table | Rows |", "| --- | ---: |"]
    lines.extend(f"| {name} | {count:,} |" for name, count in counts.items())
    lines += ["", f"Unmatched relations (either endpoint): {relations['unmatched']:,} / {relations['total']:,} ({relations['unmatched_fraction']:.4%}).",
              f"Missing source versions: {relations['missing_source_versions']:,}; missing resolved versions: {relations['missing_resolved_versions']:,}.",
              f"Requirements without exact dependency package match: {requirements['unmatched']:,} / {requirements['total']:,} ({requirements['unmatched_fraction']:.4%}).",
              "", f"Technical-lag preparation candidates: {report['technical_lag']['prepared_candidates']:,}.",
              "Candidates require the SemVer and historical eligibility checks in docs/technical-lag-definition.md; these are not calculated lag metrics.",
              "All unmatched relations remain available with endpoint flags and reasons in relation_matches.parquet.",
              "The npm sample is not dependency-closed. Missing external packuments cannot provide release history.",
              "deps.dev resolution is an observed graph, not historical v_req or a lockfile.",
              "", f"Integrity checks: {len(report['integrity_violations'])}, all zero violations.",
              f"Skipped raw graph edges: {report['raw_statistics']['depsdev_skipped_edges']:,}.",
              f"Parser warnings: {report['raw_statistics']['parser_warnings']:,}; details in warnings.jsonl.",
              f"Version quality: {json.dumps(report['version_quality'], sort_keys=True)}.",
              "", "Timestamp strings preserve source precision. repository and dist_tags are canonical JSON strings in Parquet.",
              "Input archive hashes, parser fingerprint and output checksums are in normalization-manifest.json.", ""]
    return "\n".join(lines)


def run(dataset: Path, output_dir: Path | None = None):
    import duckdb
    import pyarrow as pa
    import pyarrow.parquet as pq

    dataset = dataset.resolve()
    manifest_path = dataset / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    destination = (output_dir if output_dir is not None else dataset.with_name(dataset.name + "-normalized") / "normalized").resolve()
    if destination == dataset or dataset in destination.parents or destination in dataset.parents:
        raise ValueError("output must be separate from the immutable input dataset")
    fingerprint = parser_version()
    semver_version = subprocess.run(["node", "-p", "require('./node_modules/semver/package.json').version"],
                                   cwd=Path(__file__).parent, capture_output=True, text=True, check=True).stdout.strip()
    if semver_version != "7.8.4":
        raise ValueError("normalization requires semver@7.8.4; run npm ci --prefix processing/normalization")
    identity = {"input_manifest_sha256": digest(manifest_path), "parser_version": fingerprint,
                "dependencies": {"pyarrow": pa.__version__, "duckdb": duckdb.__version__, "semver": semver_version}}
    if destination.exists():
        previous = json.loads((destination / "normalization-manifest.json").read_text())
        if previous["build"] != identity:
            raise ValueError("existing Parquet build has different inputs/parser/runtime; use a separate dataset copy")
        for group in ("raw", "audit"):
            for source in ("npm", "depsdev"):
                for shard in manifest["shards"][group][source]:
                    if digest(dataset / safe_path(shard["path"])) != shard["sha256"]:
                        raise ValueError(f"input archive SHA-256 mismatch: {shard['path']}")
        for shard in manifest["shards"].get("normalized", {}).get("dependency_relation", []):
            if digest(dataset / safe_path(shard["path"])) != shard["sha256"]:
                raise ValueError(f"provenance shard SHA-256 mismatch: {shard['path']}")
        for file in previous["outputs"]:
            if digest(destination / safe_path(file["path"])) != file["sha256"]:
                raise ValueError(f"output SHA-256 mismatch: {file['path']}")
        return previous["report"]
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".normalization-", dir=destination.parent))
    writers = {}
    try:
        writers = {name: Writer(staging / name, schema, pa, pq) for name, schema in schemas(pa).items()}
        stats = Counter(npm_raw_responses=0, depsdev_raw_responses=0, depsdev_raw_edges=0,
                        depsdev_skipped_edges=0, parser_warnings=0)
        with (staging / "warnings.jsonl").open("w", encoding="utf-8") as warnings_file:
            for source in ("npm", "depsdev"):
                for number, (document, provenance, state) in enumerate(raw_records(dataset, manifest, source, fingerprint), 1):
                    stats[source + "_raw_responses"] += 1
                    if source == "npm":
                        package, versions, requirements, warnings = npm.normalize_packument(document, provenance)
                        if package["package_name"] != state["requested_name"]:
                            raise ValueError("npm package identity mismatch")
                        writers["packages"].add(package)
                        for record in versions:
                            writers["versions"].add(record)
                        for record in requirements:
                            writers["dependency_requirements"].add(record)
                    else:
                        graph = depsdev.normalize_graph(document, provenance)
                        if (graph.root_package, graph.root_version) != (state["requested_package"], state["requested_version"]):
                            raise ValueError("deps.dev root identity mismatch")
                        stats["depsdev_raw_edges"] += graph.edges
                        stats["depsdev_skipped_edges"] += graph.skipped_edges
                        if provenance["retrieved_at"] is None:
                            if graph.records:
                                raise ValueError("nonempty graph has no exact retrieved_at in state or legacy provenance")
                            stats["empty_graphs_without_retrieval_time"] += 1
                        warnings = graph.warnings
                        for record in graph.records:
                            writers["dependency_relations"].add(record)
                    for warning in warnings:
                        stats["parser_warnings"] += 1
                        warnings_file.write(json.dumps({"raw_path": provenance["raw_path"], "warning": warning}, ensure_ascii=False) + "\n")
                    if number % 500 == 0:
                        LOG.info("%s raw responses normalized: %s", source, number)
        for writer in writers.values():
            writer.close()
        writers.clear()
        LOG.info("Checking keys, foreign keys and cross-source matches")
        report = analyze(staging, duckdb)
        report["raw_statistics"] = dict(stats)
        if report["counts"]["dependency_relations"] + stats["depsdev_skipped_edges"] != stats["depsdev_raw_edges"]:
            raise ValueError("raw graph edge accounting mismatch")
        (staging / "report.md").write_text(render_report(report), encoding="utf-8")
        outputs = [{"path": path.relative_to(staging).as_posix(), "sha256": digest(path), "bytes": path.stat().st_size}
                   for path in sorted(staging.rglob("*")) if path.is_file()]
        result = {"build": identity, "report": report, "outputs": outputs}
        (staging / "normalization-manifest.json").write_text(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        os.rename(staging, destination)
        return report
    finally:
        for writer in writers.values():
            writer.writer.close()
        if staging.exists():
            shutil.rmtree(staging)
