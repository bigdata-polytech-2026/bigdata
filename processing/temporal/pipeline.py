from __future__ import annotations

import hashlib, json, os, shutil, subprocess, tempfile
from collections import Counter
from pathlib import Path

MAX_RELATIONS = 100_000
MAX_SNAPSHOTS = 12

def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""): h.update(block)
    return h.hexdigest()

def observation_times(consumer_time: str, releases: list[str], ceiling: str, limit: int = MAX_SNAPSHOTS) -> list[str]:
    """Consumer publication plus evenly-spaced dependency-release event times."""
    values = [consumer_time] + sorted({t for t in releases if consumer_time < t <= ceiling})
    values = list(dict.fromkeys(values))
    if len(values) <= limit: return values
    indexes = [i * (len(values) - 1) // (limit - 1) for i in range(limit)]
    return [values[i] for i in indexes]

def _verify(root: Path, outputs: list[dict], label: str):
    for item in outputs:
        p = root / item["path"]
        if not p.is_file() or digest(p) != item["sha256"]: raise ValueError(f"{label} SHA-256 mismatch: {item['path']}")

def run(input_dir: Path, output_dir: Path, relation_limit: int = MAX_RELATIONS, snapshots_per_relation: int = MAX_SNAPSHOTS):
    import duckdb, pyarrow as pa, pyarrow.parquet as pq
    input_dir, output_dir = input_dir.resolve(), output_dir.resolve()
    manifest_path = input_dir / "normalization-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    _verify(input_dir, manifest["outputs"], "normalized input")
    identity = {"normalization_manifest_sha256": digest(manifest_path), "relation_limit": relation_limit, "snapshots_per_relation": snapshots_per_relation, "pipeline_sha256": digest(Path(__file__)), "lag_runner_sha256": digest(Path(__file__).parents[1] / "technical_lag" / "runner.cjs")}
    if output_dir.exists():
        old = json.loads((output_dir / "temporal-manifest.json").read_text())
        if old["build"] != identity: raise ValueError("existing temporal output has different inputs/configuration")
        _verify(output_dir, old["outputs"], "temporal output")
        return old["report"]
    output_dir.parent.mkdir(parents=True, exist_ok=True); stage = Path(tempfile.mkdtemp(prefix=".temporal-", dir=output_dir.parent))
    db = duckdb.connect()
    try:
        q = lambda p: "'" + str(p).replace("'", "''") + "'"
        db.execute(f"CREATE VIEW inputs AS SELECT * FROM read_parquet({q(input_dir / 'technical_lag_inputs.parquet')})")
        db.execute(f"CREATE VIEW versions AS SELECT * FROM read_parquet({q(input_dir / 'versions/*.parquet')})")
        db.execute(f"CREATE VIEW packages AS SELECT * FROM read_parquet({q(input_dir / 'packages/*.parquet')})")
        candidates = db.execute(f"""SELECT source_package, source_version, dependency_name, requirement, dependency_type,
            observation_time AS consumer_published_at, consumer_provenance.retrieved_at AS consumer_retrieved_at,
            dependency_provenance.retrieved_at AS dependency_retrieved_at, shadowed_by_optional
            FROM inputs WHERE preparation_exclusion_reason IS NULL AND dependency_package_matched
            ORDER BY hash(source_package, source_version, dependency_type, dependency_name) LIMIT {int(relation_limit)}""").fetchall()
        cols = [d[0] for d in db.description]; candidates = [dict(zip(cols, row)) for row in candidates]
        names = sorted({r["dependency_name"] for r in candidates})
        histories = {}
        for name in names:
            rows = db.execute("SELECT version, published_at FROM versions WHERE package_name=? ORDER BY published_at, version", [name]).fetchall()
            prov = db.execute("SELECT provenance FROM packages WHERE package_name=?", [name]).fetchone()[0]
            histories[name] = {"package_name": name, "provenance": prov, "versions": [{"version": v, "published_at": t} for v,t in rows]}
        hist = stage / "histories.jsonl"; diag = stage / "diagnostics.jsonl"; candidate_file = stage / "candidates.jsonl"; results = stage / "results.jsonl"
        with hist.open("w") as f:
            for value in histories.values(): f.write(json.dumps(value, separators=(",", ":")) + "\n")
        observations = []
        with candidate_file.open("w") as f:
            for row in candidates:
                ceiling = min(row["consumer_retrieved_at"], row["dependency_retrieved_at"])
                releases = [x["published_at"] for x in histories[row["dependency_name"]]["versions"] if x["published_at"]]
                for t in observation_times(row["consumer_published_at"], releases, ceiling, snapshots_per_relation):
                    value = {**row, "observation_time": t, "consumer_retrieved_at": row["consumer_retrieved_at"], "dependency_package_matched": True}
                    observations.append(value); f.write(json.dumps(value, separators=(",", ":")) + "\n")
        runner = Path(__file__).parents[1] / "technical_lag" / "runner.cjs"
        with candidate_file.open("rb") as source, results.open("wb") as target:
            subprocess.run(["node", str(runner), str(hist), str(diag)], stdin=source, stdout=target, check=True)
        computed = [json.loads(line) for line in results.read_text().splitlines()]
        if len(computed) != len(observations): raise ValueError("temporal calculator row accounting mismatch")
        rows = [{**obs, **calc, "historical_mode": "reconstructed_survivors"} for obs, calc in zip(observations, computed)]
        schema = pa.schema([(k, pa.string()) for k in ("source_package","source_version","dependency_name","requirement","dependency_type","consumer_published_at","consumer_retrieved_at","dependency_retrieved_at","observation_time","effective_dependency_type","status","exclusion_reason","v_req","v_latest","time_lag_nanoseconds","update_type","historical_mode")] + [("shadowed_by_optional",pa.bool_()),("available_versions",pa.int64()),("version_lag",pa.int64()),("time_lag_days",pa.float64()),("nonmonotone_publish_order",pa.bool_())])
        pq.write_table(pa.Table.from_pylist(rows, schema=schema), stage / "temporal_technical_lag.parquet", compression="zstd")
        relation_counts = Counter((r["source_package"],r["source_version"],r["dependency_type"],r["dependency_name"]) for r in observations)
        coverage = {"selected_relations": len(candidates), "temporal_observations": len(rows), "max_snapshots_per_relation": snapshots_per_relation, "relations_with_multiple_states": sum(count > 1 for count in relation_counts.values()), "successful_observations": sum(r["status"] == "ok" for r in rows), "observation_time_start": min((r["observation_time"] for r in observations), default=None), "observation_time_end": max((r["observation_time"] for r in observations), default=None), "snapshots_per_relation": dict(sorted(Counter(relation_counts.values()).items()))}
        (stage / "temporal-statistics.json").write_text(json.dumps(coverage, indent=2, sort_keys=True) + "\n")
        (stage / "report.md").write_text("# Temporal pilot\n\n" + json.dumps(coverage, indent=2) + "\n\nObservation times are dependency release events and never exceed either Registry snapshot retrieval time.\n")
        for p in (hist, diag, candidate_file, results): p.unlink(missing_ok=True)
        outputs = [{"path": p.name, "sha256": digest(p), "bytes": p.stat().st_size} for p in stage.iterdir() if p.is_file()]
        (stage / "temporal-manifest.json").write_text(json.dumps({"build": identity, "report": coverage, "outputs": outputs}, indent=2, sort_keys=True) + "\n")
        os.replace(stage, output_dir); return coverage
    finally:
        db.close()
        if stage.exists(): shutil.rmtree(stage)
