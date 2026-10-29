from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import shutil
import tempfile
from collections import defaultdict
from pathlib import Path, PurePosixPath
from typing import Any, Iterable


SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?(?:\+[0-9A-Za-z.-]+)?$")


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def safe_path(value: str) -> Path:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValueError(f"unsafe manifest path: {value}")
    return Path(*path.parts)


def _semver(value: str) -> tuple | None:
    match = SEMVER.fullmatch(value)
    if not match:
        return None
    prerelease = match.group(4)
    parts = []
    if prerelease is not None:
        for part in prerelease.split("."):
            parts.append((0, int(part)) if part.isdigit() else (1, part))
    return int(match.group(1)), int(match.group(2)), int(match.group(3)), tuple(parts), prerelease is None


def _compare(left: str, right: str) -> int | None:
    a, b = _semver(left), _semver(right)
    if a is None or b is None:
        return None
    if a[:3] != b[:3]:
        return -1 if a[:3] < b[:3] else 1
    if a[4] != b[4]:
        return 1 if a[4] else -1
    for x, y in zip(a[3], b[3]):
        if x == y:
            continue
        if x[0] != y[0]:
            return -1 if x[0] == 0 else 1
        return -1 if x[1] < y[1] else 1
    return (a[3] > b[3]) - (a[3] < b[3])


def affected_by_range(version: str, range_value: dict[str, Any]) -> tuple[bool | None, str | None]:
    """Apply OSV SEMVER/ECOSYSTEM event intervals; None means an unusable range."""
    if range_value.get("type") not in {"SEMVER", "ECOSYSTEM"}:
        return False, None
    if _semver(version) is None:
        return None, "invalid_package_version"
    active = False
    for event in range_value.get("events", []):
        if not isinstance(event, dict) or len(event) != 1:
            return None, "malformed_range_event"
        kind, boundary = next(iter(event.items()))
        if not isinstance(boundary, str):
            return None, "malformed_range_event"
        if kind == "introduced":
            if boundary == "0":
                active = True
            else:
                comparison = _compare(version, boundary)
                if comparison is None:
                    return None, "invalid_range_boundary"
                active = comparison >= 0
        elif kind in {"fixed", "limit"}:
            comparison = _compare(version, boundary)
            if comparison is None:
                return None, "invalid_range_boundary"
            if comparison >= 0:
                active = False
        elif kind == "last_affected":
            comparison = _compare(version, boundary)
            if comparison is None:
                return None, "invalid_range_boundary"
            if comparison > 0:
                active = False
        else:
            return None, "unsupported_range_event"
    return active, None


def record_matches_version(record: dict[str, Any], version: str) -> tuple[bool | None, str | None]:
    affected = record["affected"]
    if version in affected["versions"]:
        return True, None
    matches = False
    usable = False
    for range_value in affected["ranges"]:
        result, error = affected_by_range(version, range_value)
        if error:
            return None, error
        if result is not None:
            usable = True
            matches = matches or result
    return (matches if usable else False), None


def _read_osv_records(dataset: Path, manifest: dict[str, Any]) -> Iterable[dict[str, Any]]:
    for shard in manifest.get("shards", {}).get("normalized", {}).get("vulnerability", []):
        path = dataset / safe_path(shard["path"])
        if digest(path) != shard["sha256"]:
            raise ValueError(f"OSV shard SHA-256 mismatch: {shard['path']}")
        count = 0
        with gzip.open(path, "rt", encoding="utf-8") as stream:
            for line in stream:
                if line.strip():
                    count += 1
                    yield json.loads(line)
        if count != shard["rows"]:
            raise ValueError(f"OSV shard row count mismatch: {shard['path']}")


def _record_error(record: Any) -> str | None:
    if not isinstance(record, dict) or record.get("schema_version") != "2.0.0" or record.get("source") != "osv":
        return "invalid_osv_contract"
    if record.get("ecosystem") != "npm" or not isinstance(record.get("package_name"), str) or not record["package_name"]:
        return "invalid_osv_package"
    if not isinstance(record.get("osv_id"), str) or not isinstance(record.get("affected_index"), int):
        return "invalid_osv_identity"
    affected = record.get("affected")
    if not isinstance(affected, dict) or not isinstance(affected.get("versions"), list) or not isinstance(affected.get("ranges"), list):
        return "invalid_affected_shape"
    if not all(isinstance(value, str) for value in affected["versions"]):
        return "invalid_affected_version"
    return None


def _canonical(record: dict[str, Any]) -> str:
    copy = {key: value for key, value in record.items() if key != "provenance"}
    return json.dumps(copy, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _schema(pa):
    text = pa.string()
    return {
        "security_features": pa.schema([
            ("package_name", text), ("version", text), ("is_vulnerable", pa.bool_()),
            ("osv_ids", text), ("severity", text), ("has_fixed_version", pa.bool_()),
            ("matched_osv_records", pa.int64()), ("unresolved_osv_records", pa.int64()),
        ]),
        "mapping_errors": pa.schema([
            ("error_reason", text), ("osv_id", text), ("affected_index", pa.int64()),
            ("package_name", text), ("record", text),
        ]),
    }


def _atomic_directory(staging: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    os.replace(staging, destination)


def run(dataset: Path, versions_dir: Path, output_dir: Path) -> dict[str, Any]:
    import pyarrow as pa
    import pyarrow.parquet as pq

    dataset, versions_dir, output_dir = dataset.resolve(), versions_dir.resolve(), output_dir.resolve()
    if output_dir in {dataset, versions_dir} or dataset in output_dir.parents or versions_dir in output_dir.parents:
        raise ValueError("output must be separate from immutable inputs")
    manifest_path = dataset / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    versions_path = versions_dir / "versions" / "part-00000.parquet"
    if not versions_path.is_file():
        raise ValueError(f"missing normalized npm versions: {versions_path}")
    if output_dir.exists():
        previous = json.loads((output_dir / "security-manifest.json").read_text(encoding="utf-8"))
        identity = {"pilot_manifest_sha256": digest(manifest_path), "versions_sha256": digest(versions_path)}
        if previous["inputs"] != identity:
            raise ValueError("existing security output has different inputs")
        for item in previous.get("outputs", []):
            path = output_dir / safe_path(item["path"])
            if not path.is_file() or digest(path) != item["sha256"]:
                raise ValueError(f"security output SHA-256 mismatch: {item['path']}")
        return previous["report"]

    versions = pq.read_table(versions_path, columns=["package_name", "version"]).to_pylist()
    names = {row["package_name"] for row in versions}
    errors, records_by_package, seen = [], defaultdict(list), {}
    raw_records = 0
    duplicate_records = 0
    for record in _read_osv_records(dataset, manifest):
        raw_records += 1
        reason = _record_error(record)
        if reason is None and record["package_name"] not in names:
            reason = "unmatched_osv_package"
        identity = (record.get("osv_id"), record.get("affected_index"), record.get("package_name")) if isinstance(record, dict) else (None, None, None)
        if reason is None:
            canonical = _canonical(record)
            previous = seen.get(identity)
            if previous is not None:
                if previous != canonical:
                    reason = "ambiguous_duplicate_osv_record"
                else:
                    duplicate_records += 1
                    continue
            else:
                seen[identity] = canonical
        if reason is not None:
            errors.append({"error_reason": reason, "osv_id": identity[0], "affected_index": identity[1], "package_name": identity[2], "record": json.dumps(record, ensure_ascii=False, sort_keys=True)})
        else:
            records_by_package[record["package_name"]].append(record)

    schemas = _schema(pa)
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".security-", dir=output_dir.parent))
    try:
        features, match_errors = [], []
        vulnerable = 0
        for row in versions:
            matched, unresolved = [], 0
            for record in records_by_package.get(row["package_name"], []):
                result, reason = record_matches_version(record, row["version"])
                if reason:
                    unresolved += 1
                    match_errors.append({"error_reason": reason, "osv_id": record["osv_id"], "affected_index": record["affected_index"], "package_name": record["package_name"], "record": json.dumps(record, ensure_ascii=False, sort_keys=True)})
                elif result:
                    matched.append(record)
            osv_ids = sorted({record["osv_id"] for record in matched})
            is_vulnerable = bool(osv_ids)
            vulnerable += is_vulnerable
            severity = [item for record in matched for item in record.get("severity", [])]
            features.append({"package_name": row["package_name"], "version": row["version"], "is_vulnerable": is_vulnerable,
                             "osv_ids": json.dumps(osv_ids, ensure_ascii=False), "severity": json.dumps(severity, ensure_ascii=False, sort_keys=True),
                             "has_fixed_version": any(record.get("fixed_versions") for record in matched),
                             "matched_osv_records": len(matched), "unresolved_osv_records": unresolved})
        pq.write_table(pa.Table.from_pylist(features, schema=schemas["security_features"]), staging / "security_features.parquet", compression="zstd")
        all_errors = errors + match_errors
        pq.write_table(pa.Table.from_pylist(all_errors, schema=schemas["mapping_errors"]), staging / "mapping_errors.parquet", compression="zstd")
        error_reasons = defaultdict(int)
        for row in all_errors: error_reasons[row["error_reason"]] += 1
        mapped_records = sum(len(values) for values in records_by_package.values())
        unique_records = mapped_records + len(errors)
        report = {"npm_versions": len(versions), "osv_records_input": raw_records,
                  "osv_records_deduplicated": duplicate_records, "osv_records_unique": unique_records,
                  "osv_records_mapped": mapped_records, "osv_records_unmapped": len(errors), "vulnerable_package_versions": vulnerable,
                  "vulnerable_fraction": vulnerable / len(versions) if versions else 0.0,
                  "mapping_success_fraction": mapped_records / unique_records if unique_records else 1.0,
                  "mapping_errors": dict(sorted(error_reasons.items()))}
        (staging / "mapping-statistics.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        identity = {"pilot_manifest_sha256": digest(manifest_path), "versions_sha256": digest(versions_path)}
        outputs = [{"path": path.name, "sha256": digest(path), "bytes": path.stat().st_size} for path in sorted(staging.iterdir()) if path.is_file()]
        (staging / "security-manifest.json").write_text(json.dumps({"inputs": identity, "report": report, "outputs": outputs}, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        _atomic_directory(staging, output_dir)
        return report
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
