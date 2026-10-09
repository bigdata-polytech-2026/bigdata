from __future__ import annotations

import dataclasses
import datetime as dt
import gzip
import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import tarfile
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Tuple

from collector.depsdev.collector import DEFAULT_API_URL as DEFAULT_DEPSDEV_API_URL
from collector.depsdev.collector import DepsDevCollector
from collector.npm.collector import DEFAULT_QUERY, NpmCollector
from collector.osv.collector import OsvCollector


LOGGER = logging.getLogger("collector.pilot")
PILOT_VERSION = "1.0.0"


@dataclasses.dataclass(frozen=True)
class VersionIndexStats:
    packages: int
    package_versions: int
    dependency_requirements: int
    selected_versions: int


@dataclasses.dataclass
class PilotSummary:
    run_id: str
    dataset_id: str
    status: str
    package_target: int
    packages: int = 0
    package_versions: int = 0
    dependency_requirements: int = 0
    selected_versions: int = 0
    depsdev_successes: int = 0
    depsdev_failures: int = 0
    depsdev_success_rate: float = 0.0
    dependency_relations: int = 0
    osv_checked: int = 0
    osv_errors: int = 0
    osv_success_rate: float = 0.0
    versions_with_vulnerabilities: int = 0
    unique_osv_records: int = 0
    vulnerability_records: int = 0
    landing_bytes: int = 0
    snapshot_payload_bytes: int = 0
    raw_shards: int = 0
    normalized_shards: int = 0
    audit_shards: int = 0
    criteria_met: bool = False
    elapsed_seconds: float = 0.0
    landing_path: str = ""
    snapshot_path: str = ""
    manifest_path: str = ""
    report_path: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _json_bytes(value: Any, pretty: bool = False) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2 if pretty else None,
            sort_keys=pretty,
            separators=None if pretty else (",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _state_key(*values: str) -> str:
    return hashlib.sha256("\0".join(values).encode("utf-8")).hexdigest()


def _read_json_object(path: Path) -> Dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object in {path}")
    return value


def _chunks(values: Iterable[Tuple[str, str]], size: int) -> Iterator[List[Tuple[str, str]]]:
    chunk: List[Tuple[str, str]] = []
    for value in values:
        chunk.append(value)
        if len(chunk) == size:
            yield chunk
            chunk = []
    if chunk:
        yield chunk


def build_version_index(
    landing_dir: Path,
    package_names: Sequence[str],
    versions_per_package: int,
    database_path: Path,
    selected_path: Path,
) -> VersionIndexStats:
    """Build a disk-backed, deterministic index from active npm state markers."""
    database_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = database_path.with_name(f".{database_path.name}.{uuid.uuid4().hex}.tmp")
    connection = sqlite3.connect(str(temporary))
    succeeded = False
    requirements = 0
    try:
        connection.execute("PRAGMA journal_mode=OFF")
        connection.execute("PRAGMA synchronous=OFF")
        connection.execute(
            "CREATE TABLE package_versions ("
            "package_name TEXT NOT NULL, version TEXT NOT NULL, published_at TEXT, ordinal INTEGER NOT NULL, "
            "PRIMARY KEY (package_name, version))"
        )
        connection.execute(
            "CREATE TABLE selected_versions (package_name TEXT NOT NULL, version TEXT NOT NULL, "
            "PRIMARY KEY (package_name, version))"
        )
        insert = "INSERT OR IGNORE INTO package_versions(package_name, version, published_at, ordinal) VALUES (?, ?, ?, ?)"
        for requested_name in package_names:
            state_path = landing_dir / "state" / "npm" / f"{_state_key(requested_name)}.json"
            state = _read_json_object(state_path)
            if state.get("requested_name") != requested_name:
                raise ValueError(f"npm state identity mismatch in {state_path}")
            requirements += int(state.get("requirements", 0))
            normalized_paths = state.get("normalized_paths")
            if not isinstance(normalized_paths, list):
                raise ValueError(f"npm state has no normalized paths in {state_path}")
            version_paths = [value for value in normalized_paths if isinstance(value, str) and "/package_version/" in f"/{value}/"]
            if len(version_paths) != 1:
                raise ValueError(f"npm state must reference one package_version file in {state_path}")
            batch = []
            ordinal = 0
            with (landing_dir / version_paths[0]).open("r", encoding="utf-8") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    record = json.loads(line)
                    package_name, version = record.get("package_name"), record.get("version")
                    if not isinstance(package_name, str) or not isinstance(version, str):
                        raise ValueError(f"invalid package version record in {version_paths[0]}")
                    batch.append((package_name, version, record.get("published_at"), ordinal))
                    ordinal += 1
                    if len(batch) == 1000:
                        connection.executemany(insert, batch)
                        batch = []
            if batch:
                connection.executemany(insert, batch)

        package_cursor = connection.execute("SELECT DISTINCT package_name FROM package_versions ORDER BY package_name")
        indexed_packages = [row[0] for row in package_cursor]
        for package_name in indexed_packages:
            rows = connection.execute(
                "SELECT version FROM package_versions WHERE package_name = ? "
                "ORDER BY CASE WHEN published_at IS NULL THEN 1 ELSE 0 END, published_at, ordinal, version",
                (package_name,),
            ).fetchall()
            if versions_per_package == 0 or len(rows) <= versions_per_package:
                selected = rows
            elif versions_per_package == 1:
                selected = [rows[-1]]
            else:
                indices = [index * (len(rows) - 1) // (versions_per_package - 1) for index in range(versions_per_package)]
                selected = [rows[index] for index in indices]
            connection.executemany(
                "INSERT OR IGNORE INTO selected_versions(package_name, version) VALUES (?, ?)",
                [(package_name, row[0]) for row in selected],
            )
        connection.commit()
        package_versions = int(connection.execute("SELECT COUNT(*) FROM package_versions").fetchone()[0])
        selected_versions = int(connection.execute("SELECT COUNT(*) FROM selected_versions").fetchone()[0])

        selected_lines = []
        for package_name, version in connection.execute("SELECT package_name, version FROM selected_versions ORDER BY package_name, version"):
            selected_lines.append(f"{package_name}@{version}\n")
        _atomic_write(selected_path, "".join(selected_lines).encode("utf-8"))
        succeeded = True
    finally:
        connection.close()
        if not succeeded and temporary.exists():
            temporary.unlink()
    os.replace(temporary, database_path)
    return VersionIndexStats(
        packages=len(indexed_packages),
        package_versions=package_versions,
        dependency_requirements=requirements,
        selected_versions=selected_versions,
    )


def iter_selected_versions(database_path: Path) -> Iterator[Tuple[str, str]]:
    connection = sqlite3.connect(str(database_path))
    try:
        cursor = connection.execute("SELECT package_name, version FROM selected_versions ORDER BY package_name, version")
        for package_name, version in cursor:
            yield package_name, version
    finally:
        connection.close()


class PilotCollector:
    def __init__(
        self,
        data_root: Path,
        dataset_id: str = "pilot-v1",
        package_limit: int = 3000,
        versions_per_package: int = 5,
        chunk_size: int = 500,
        workers: int = 8,
        osv_workers: int = 8,
        timeout: float = 30.0,
        retries: int = 3,
        shard_size_mb: int = 128,
        min_depsdev_success_rate: float = 0.90,
        min_osv_success_rate: float = 0.99,
        npm_query: str = DEFAULT_QUERY,
        npm_registry_url: str = "https://registry.npmjs.org",
        depsdev_api_url: str = DEFAULT_DEPSDEV_API_URL,
        osv_api_url: str = "https://api.osv.dev",
    ):
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", dataset_id) is None or dataset_id in {".", ".."}:
            raise ValueError("invalid dataset id")
        if package_limit < 1 or chunk_size < 1 or workers < 1 or osv_workers < 1 or shard_size_mb < 1:
            raise ValueError("package limit, chunk size, workers, and shard size must be positive")
        if versions_per_package < 0 or retries < 0:
            raise ValueError("versions per package and retries must be non-negative")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if not 0.0 <= min_depsdev_success_rate <= 1.0 or not 0.0 <= min_osv_success_rate <= 1.0:
            raise ValueError("success rates must be between 0 and 1")
        self.data_root = Path(data_root)
        self.dataset_id = dataset_id
        self.package_limit = package_limit
        self.versions_per_package = versions_per_package
        self.chunk_size = chunk_size
        self.workers = workers
        self.osv_workers = osv_workers
        self.timeout = timeout
        self.retries = retries
        self.shard_size_bytes = shard_size_mb * 1024 * 1024
        self.min_depsdev_success_rate = min_depsdev_success_rate
        self.min_osv_success_rate = min_osv_success_rate
        self.npm_query = npm_query
        self.npm_registry_url = npm_registry_url.rstrip("/")
        self.depsdev_api_url = depsdev_api_url.rstrip("/")
        self.osv_api_url = osv_api_url.rstrip("/")
        self.landing_dir = self.data_root / "landing" / dataset_id
        self.snapshot_dir = self.data_root / "datasets" / dataset_id

    def run(self) -> PilotSummary:
        existing = self._existing_summary()
        if existing is not None:
            return existing

        started = time.monotonic()
        run_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid.uuid4().hex[:8]
        summary = PilotSummary(
            run_id=run_id,
            dataset_id=self.dataset_id,
            status="running",
            package_target=self.package_limit,
            landing_path=self._relative_to_data_root(self.landing_dir),
        )
        self._check_disk_space()
        self.landing_dir.mkdir(parents=True, exist_ok=True)
        inputs_dir = self.landing_dir / "inputs"
        packages_path = inputs_dir / "packages.txt"
        index_path = inputs_dir / "version-index.sqlite"
        selected_path = inputs_dir / "enrichment-package-versions.txt"
        package_names = self._load_package_names(packages_path)

        LOGGER.info("pilot %s: collecting npm metadata for %d packages", self.dataset_id, self.package_limit)
        npm_collector = NpmCollector(
            data_dir=self.landing_dir,
            registry_url=self.npm_registry_url,
            workers=self.workers,
            timeout=self.timeout,
            retries=self.retries,
        )
        npm_summary = npm_collector.run(
            limit=self.package_limit if package_names is None else len(package_names),
            query=self.npm_query,
            package_names=package_names,
        )
        if npm_summary.available < npm_summary.target:
            summary.packages = npm_summary.available
            summary.status = "incomplete"
            summary.elapsed_seconds = round(time.monotonic() - started, 3)
            self._write_working_report(summary, {"npm": [npm_summary.manifest_path]}, [], [])
            return summary
        if package_names is None:
            package_names = self._freeze_package_names(npm_summary.manifest_path, packages_path)

        LOGGER.info("pilot %s: indexing full npm version history on disk", self.dataset_id)
        index_stats = build_version_index(
            self.landing_dir,
            package_names,
            self.versions_per_package,
            index_path,
            selected_path,
        )
        summary.packages = index_stats.packages
        summary.package_versions = index_stats.package_versions
        summary.dependency_requirements = index_stats.dependency_requirements
        summary.selected_versions = index_stats.selected_versions

        manifests: Dict[str, List[str]] = {"npm": [npm_summary.manifest_path], "depsdev": [], "osv": []}
        depsdev_failures: List[str] = []
        osv_failures: List[str] = []
        LOGGER.info("pilot %s: enriching %d sampled versions with deps.dev", self.dataset_id, summary.selected_versions)
        for number, chunk in enumerate(_chunks(iter_selected_versions(index_path), self.chunk_size), start=1):
            LOGGER.info("pilot %s: deps.dev chunk %d (%d versions)", self.dataset_id, number, len(chunk))
            chunk_summary = DepsDevCollector(
                data_dir=self.landing_dir,
                api_url=self.depsdev_api_url,
                workers=self.workers,
                timeout=self.timeout,
                retries=self.retries,
            ).run(chunk)
            manifests["depsdev"].append(chunk_summary.manifest_path)
            summary.depsdev_successes += chunk_summary.api_successes
            summary.depsdev_failures += chunk_summary.api_failures
            summary.dependency_relations += chunk_summary.normalized_edges
            depsdev_failures.extend(self._failed_versions(chunk_summary.manifest_path, "failed"))

        LOGGER.info("pilot %s: enriching %d sampled versions with OSV", self.dataset_id, summary.selected_versions)
        osv_ids = set()
        for number, chunk in enumerate(_chunks(iter_selected_versions(index_path), self.chunk_size), start=1):
            LOGGER.info("pilot %s: OSV chunk %d (%d versions)", self.dataset_id, number, len(chunk))
            chunk_summary = OsvCollector(
                data_dir=self.landing_dir,
                api_url=self.osv_api_url,
                workers=self.osv_workers,
                timeout=self.timeout,
                retries=self.retries,
            ).run(chunk)
            manifests["osv"].append(chunk_summary.manifest_path)
            summary.osv_checked += chunk_summary.checked
            summary.osv_errors += chunk_summary.api_errors
            summary.versions_with_vulnerabilities += chunk_summary.versions_with_vulnerabilities
            summary.vulnerability_records += chunk_summary.vulnerability_records
            manifest = _read_json_object(self.landing_dir / chunk_summary.manifest_path)
            for result in manifest.get("results", []):
                if isinstance(result, dict):
                    ids = result.get("osv_ids")
                    if isinstance(ids, list):
                        osv_ids.update(value for value in ids if isinstance(value, str))
            osv_failures.extend(self._failed_versions(chunk_summary.manifest_path, "api_error"))
        summary.unique_osv_records = len(osv_ids)

        summary.depsdev_success_rate = round(summary.depsdev_successes / summary.selected_versions, 6) if summary.selected_versions else 0.0
        summary.osv_success_rate = round(summary.osv_checked / summary.selected_versions, 6) if summary.selected_versions else 0.0
        summary.criteria_met = (
            summary.packages == self.package_limit
            and summary.selected_versions > 0
            and summary.depsdev_success_rate >= self.min_depsdev_success_rate
            and summary.osv_success_rate >= self.min_osv_success_rate
        )
        summary.status = "complete" if summary.criteria_met else "incomplete"
        summary.elapsed_seconds = round(time.monotonic() - started, 3)
        self._write_failure_list(self.landing_dir / "reports" / "depsdev-failures.txt", depsdev_failures)
        self._write_failure_list(self.landing_dir / "reports" / "osv-failures.txt", osv_failures)

        if summary.criteria_met:
            LOGGER.info("pilot %s: compacting immutable snapshot", self.dataset_id)
            summary.snapshot_path = self._relative_to_data_root(self.snapshot_dir)
            summary.manifest_path = f"{summary.snapshot_path}/manifest.json"
            summary.report_path = f"{summary.snapshot_path}/report.md"
            self._build_snapshot(summary, manifests, package_names, index_path, packages_path, selected_path, started)
        else:
            self._write_working_report(summary, manifests, depsdev_failures, osv_failures)
        return summary

    def _config(self) -> Dict[str, Any]:
        return {
            "pilot_version": PILOT_VERSION,
            "dataset_id": self.dataset_id,
            "package_limit": self.package_limit,
            "versions_per_package": self.versions_per_package,
            "chunk_size": self.chunk_size,
            "workers": self.workers,
            "osv_workers": self.osv_workers,
            "timeout": self.timeout,
            "retries": self.retries,
            "shard_size_bytes": self.shard_size_bytes,
            "min_depsdev_success_rate": self.min_depsdev_success_rate,
            "min_osv_success_rate": self.min_osv_success_rate,
            "npm_query": self.npm_query,
            "npm_registry_url": self.npm_registry_url,
            "depsdev_api_url": self.depsdev_api_url,
            "osv_api_url": self.osv_api_url,
        }

    def _existing_summary(self) -> Optional[PilotSummary]:
        manifest_path = self.snapshot_dir / "manifest.json"
        if not manifest_path.is_file():
            return None
        manifest = _read_json_object(manifest_path)
        if manifest.get("config") != self._config():
            raise ValueError(f"immutable snapshot {self.snapshot_dir} already exists with different configuration")
        self._validate_built_snapshot(self.snapshot_dir, manifest)
        summary = manifest.get("summary")
        if not isinstance(summary, dict):
            raise ValueError(f"snapshot manifest has no summary: {manifest_path}")
        return PilotSummary(**summary)

    def _load_package_names(self, path: Path) -> Optional[List[str]]:
        try:
            values = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        except FileNotFoundError:
            return None
        if len(values) != self.package_limit or len(set(values)) != len(values):
            raise ValueError(f"frozen package list in {path} does not match package limit {self.package_limit}")
        return values

    def _freeze_package_names(self, manifest_path: str, destination: Path) -> List[str]:
        manifest = _read_json_object(self.landing_dir / manifest_path)
        names = []
        seen = set()
        for result in manifest.get("results", []):
            if not isinstance(result, dict) or result.get("status") not in {"downloaded", "skipped"}:
                continue
            name = result.get("package")
            if isinstance(name, str) and name not in seen:
                seen.add(name)
                names.append(name)
        if len(names) != self.package_limit:
            raise ValueError(f"npm manifest contains {len(names)} successful packages, expected {self.package_limit}")
        _atomic_write(destination, "".join(f"{name}\n" for name in names).encode("utf-8"))
        return names

    def _failed_versions(self, manifest_path: str, status: str) -> List[str]:
        manifest = _read_json_object(self.landing_dir / manifest_path)
        result = []
        for item in manifest.get("results", []):
            if isinstance(item, dict) and item.get("status") == status:
                result.append(f"{item.get('package_name')}@{item.get('version')}\t{item.get('error', '')}")
        return result

    def _write_failure_list(self, path: Path, failures: Sequence[str]) -> None:
        _atomic_write(path, "".join(f"{value}\n" for value in failures).encode("utf-8"))

    def _write_working_report(
        self,
        summary: PilotSummary,
        manifests: Mapping[str, Sequence[str]],
        depsdev_failures: Sequence[str],
        osv_failures: Sequence[str],
    ) -> None:
        report_dir = self.landing_dir / "reports"
        summary.manifest_path = self._relative_to_data_root(report_dir / "latest-manifest.json")
        summary.report_path = self._relative_to_data_root(report_dir / "latest-report.md")
        manifest = {
            "schema_version": "1.0.0",
            "created_at": utc_now(),
            "config": self._config(),
            "summary": summary.to_dict(),
            "source_manifests": manifests,
            "failures": {"depsdev": list(depsdev_failures), "osv": list(osv_failures)},
        }
        _atomic_write(self.data_root / summary.manifest_path, _json_bytes(manifest, pretty=True))
        _atomic_write(self.data_root / summary.report_path, self._report_markdown(summary).encode("utf-8"))

    def _build_snapshot(
        self,
        summary: PilotSummary,
        manifests: Mapping[str, Sequence[str]],
        package_names: Sequence[str],
        index_path: Path,
        packages_path: Path,
        selected_path: Path,
        started: float,
    ) -> None:
        if self.snapshot_dir.exists():
            raise FileExistsError(f"immutable snapshot already exists: {self.snapshot_dir}")
        build_dir = self.snapshot_dir.with_name(f".{self.snapshot_dir.name}.{uuid.uuid4().hex}.tmp")
        build_dir.mkdir(parents=True, exist_ok=False)
        try:
            (build_dir / "inputs").mkdir(parents=True)
            shutil.copyfile(packages_path, build_dir / "inputs" / packages_path.name)
            shutil.copyfile(selected_path, build_dir / "inputs" / selected_path.name)
            raw_shards = {}
            for source in ("npm", "depsdev", "osv"):
                raw_shards[source] = self._write_tar_shards(
                    self._iter_raw_files(source, package_names, index_path),
                    build_dir / "raw" / source,
                )
            audit_shards = {}
            for source in ("npm", "depsdev", "osv"):
                audit_shards[source] = self._write_tar_shards(
                    self._iter_audit_files(source, manifests, package_names, index_path),
                    build_dir / "audit" / source,
                )
            normalized_shards = {}
            for entity in ("package", "package_version", "dependency_requirement", "dependency_relation", "vulnerability"):
                normalized_shards[entity] = self._write_jsonl_shards(
                    self._iter_normalized_files(entity, package_names, index_path),
                    build_dir / "normalized" / entity,
                )
            self._validate_counts(summary, raw_shards, normalized_shards)
            summary.landing_bytes = sum(path.stat().st_size for path in self.landing_dir.rglob("*") if path.is_file())
            summary.raw_shards = sum(len(shards) for shards in raw_shards.values())
            summary.normalized_shards = sum(len(shards) for shards in normalized_shards.values())
            summary.audit_shards = sum(len(shards) for shards in audit_shards.values())
            summary.snapshot_payload_bytes = sum(
                int(shard["compressed_bytes"])
                for group in (raw_shards, normalized_shards, audit_shards)
                for shards in group.values()
                for shard in shards
            )
            validation_dir = build_dir / "validation"
            validation_dir.mkdir(parents=True, exist_ok=True)
            for name in ("depsdev-failures.txt", "osv-failures.txt"):
                source = self.landing_dir / "reports" / name
                if source.is_file():
                    shutil.copyfile(source, validation_dir / name)
            summary.elapsed_seconds = round(time.monotonic() - started, 3)
            manifest = {
                "schema_version": "1.0.0",
                "created_at": utc_now(),
                "immutable": True,
                "config": self._config(),
                "summary": summary.to_dict(),
                "source_manifests": manifests,
                "shards": {"raw": raw_shards, "normalized": normalized_shards, "audit": audit_shards},
            }
            _atomic_write(build_dir / "report.md", self._report_markdown(summary).encode("utf-8"))
            _atomic_write(build_dir / "manifest.json", _json_bytes(manifest, pretty=True))
            self._validate_built_snapshot(build_dir, manifest)
            self.snapshot_dir.parent.mkdir(parents=True, exist_ok=True)
            os.replace(build_dir, self.snapshot_dir)
        except Exception:
            shutil.rmtree(build_dir, ignore_errors=True)
            raise

    def _check_disk_space(self) -> None:
        selected_per_package = self.versions_per_package if self.versions_per_package > 0 else 25
        estimated_bytes = (
            5 * 1024**3
            + self.package_limit * 3 * 1024**2
            + self.package_limit * selected_per_package * 100 * 1024
        )
        existing_bytes = (
            sum(path.stat().st_size for path in self.landing_dir.rglob("*") if path.is_file())
            if self.landing_dir.is_dir()
            else 0
        )
        remaining_bytes = max(5 * 1024**3, estimated_bytes - existing_bytes)
        probe = self.data_root
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        free_bytes = shutil.disk_usage(probe).free
        LOGGER.info(
            "pilot %s: free disk %.1f GiB, existing landing %.1f GiB, estimated remaining need %.1f GiB",
            self.dataset_id,
            free_bytes / 1024**3,
            existing_bytes / 1024**3,
            remaining_bytes / 1024**3,
        )
        if free_bytes < remaining_bytes:
            raise ValueError(
                f"not enough free disk for pilot: {free_bytes / 1024**3:.1f} GiB available, "
                f"approximately {remaining_bytes / 1024**3:.1f} GiB more required; reduce --package-limit or use another --data-root"
            )

    def _validate_counts(
        self,
        summary: PilotSummary,
        raw_shards: Mapping[str, Sequence[Mapping[str, Any]]],
        normalized_shards: Mapping[str, Sequence[Mapping[str, Any]]],
    ) -> None:
        row_expectations = {
            "package": summary.packages,
            "package_version": summary.package_versions,
            "dependency_requirement": summary.dependency_requirements,
            "dependency_relation": summary.dependency_relations,
        }
        for entity, expected in row_expectations.items():
            actual = sum(int(shard.get("rows", 0)) for shard in normalized_shards[entity])
            if actual != expected:
                raise ValueError(f"snapshot {entity} row count mismatch: expected {expected}, got {actual}")
        npm_raw = sum(int(shard.get("files", 0)) for shard in raw_shards["npm"])
        if npm_raw < summary.packages:
            raise ValueError(f"snapshot npm raw count is below package count: {npm_raw} < {summary.packages}")
        depsdev_raw = sum(int(shard.get("files", 0)) for shard in raw_shards["depsdev"])
        if depsdev_raw != summary.depsdev_successes:
            raise ValueError(f"snapshot depsdev raw count mismatch: expected {summary.depsdev_successes}, got {depsdev_raw}")
        osv_raw = sum(int(shard.get("files", 0)) for shard in raw_shards["osv"])
        if osv_raw < summary.osv_checked:
            raise ValueError(f"snapshot OSV raw count is below checked versions: {osv_raw} < {summary.osv_checked}")

    def _iter_raw_files(self, source: str, package_names: Sequence[str], index_path: Path) -> Iterator[Tuple[Path, str]]:
        if source == "npm":
            for name in package_names:
                state = _read_json_object(self.landing_dir / "state" / "npm" / f"{_state_key(name)}.json")
                path = self.landing_dir / state["raw_path"]
                yield path, path.relative_to(self.landing_dir).as_posix()
            search_root = self.landing_dir / "raw" / "npm-search"
            if search_root.is_dir():
                for path in sorted(search_root.rglob("*.json")):
                    yield path, path.relative_to(self.landing_dir).as_posix()
            return
        for package_name, version in iter_selected_versions(index_path):
            state_path = self.landing_dir / "state" / source / f"{_state_key(package_name, version)}.json"
            if not state_path.is_file():
                continue
            state = _read_json_object(state_path)
            if source == "osv" and isinstance(state.get("raw_pages"), list):
                raw_paths = [page.get("raw_path") for page in state["raw_pages"] if isinstance(page, dict)]
            else:
                raw_paths = [state.get("raw_path")]
            for value in raw_paths:
                if isinstance(value, str):
                    path = self.landing_dir / value
                    yield path, path.relative_to(self.landing_dir).as_posix()

    def _iter_normalized_files(self, entity: str, package_names: Sequence[str], index_path: Path) -> Iterator[Path]:
        if entity in {"package", "package_version", "dependency_requirement"}:
            needle = f"/{entity}/"
            for name in package_names:
                state = _read_json_object(self.landing_dir / "state" / "npm" / f"{_state_key(name)}.json")
                for value in state.get("normalized_paths", []):
                    if isinstance(value, str) and needle in f"/{value}/":
                        yield self.landing_dir / value
            return
        source = "depsdev" if entity == "dependency_relation" else "osv"
        for package_name, version in iter_selected_versions(index_path):
            state_path = self.landing_dir / "state" / source / f"{_state_key(package_name, version)}.json"
            if not state_path.is_file():
                continue
            state = _read_json_object(state_path)
            value = state.get("normalized_path")
            if isinstance(value, str):
                yield self.landing_dir / value

    def _iter_audit_files(
        self,
        source: str,
        manifests: Mapping[str, Sequence[str]],
        package_names: Sequence[str],
        index_path: Path,
    ) -> Iterator[Tuple[Path, str]]:
        seen = set()
        for value in manifests.get(source, []):
            path = self.landing_dir / value
            relative = path.relative_to(self.landing_dir).as_posix()
            if relative not in seen:
                seen.add(relative)
                yield path, relative
            manifest = _read_json_object(path)
            summary = manifest.get("summary")
            log_path = summary.get("log_path") if isinstance(summary, dict) else None
            if isinstance(log_path, str):
                log = self.landing_dir / log_path
                log_relative = log.relative_to(self.landing_dir).as_posix()
                if log_relative not in seen:
                    seen.add(log_relative)
                    yield log, log_relative
        if source == "npm":
            identities: Iterable[Tuple[str, ...]] = ((name,) for name in package_names)
        else:
            identities = iter_selected_versions(index_path)
        for identity in identities:
            path = self.landing_dir / "state" / source / f"{_state_key(*identity)}.json"
            if not path.is_file():
                continue
            relative = path.relative_to(self.landing_dir).as_posix()
            if relative not in seen:
                seen.add(relative)
                yield path, relative

    def _write_tar_shards(self, entries: Iterable[Tuple[Path, str]], destination: Path) -> List[Dict[str, Any]]:
        shards = []
        iterator = iter(entries)
        pending = next(iterator, None)
        number = 0
        while pending is not None:
            destination.mkdir(parents=True, exist_ok=True)
            path = destination / f"part-{number:05d}.tar.gz"
            input_bytes = 0
            files = 0
            raw_handle = path.open("xb")
            gzip_handle = gzip.GzipFile(filename="", mode="wb", compresslevel=6, fileobj=raw_handle, mtime=0)
            archive = tarfile.open(fileobj=gzip_handle, mode="w|")
            try:
                while pending is not None:
                    source_path, archive_name = pending
                    archive.add(source_path, arcname=archive_name, recursive=False)
                    input_bytes += source_path.stat().st_size
                    files += 1
                    gzip_handle.flush()
                    raw_handle.flush()
                    pending = next(iterator, None)
                    if raw_handle.tell() >= self.shard_size_bytes:
                        break
            finally:
                archive.close()
                gzip_handle.close()
                raw_handle.close()
            shards.append(self._shard_record(path, destination.parent.parent, files=files, rows=None, input_bytes=input_bytes))
            number += 1
        return shards

    def _write_jsonl_shards(self, paths: Iterable[Path], destination: Path) -> List[Dict[str, Any]]:
        shards = []
        output = None
        gzip_handle = None
        raw_handle = None
        rows = input_bytes = number = 0

        def close_shard() -> None:
            nonlocal output, gzip_handle, raw_handle, rows, input_bytes, number
            if output is None or gzip_handle is None or raw_handle is None:
                return
            gzip_handle.close()
            raw_handle.close()
            shards.append(self._shard_record(output, destination.parent.parent, files=None, rows=rows, input_bytes=input_bytes))
            output = gzip_handle = raw_handle = None
            rows = input_bytes = 0
            number += 1

        for source_path in paths:
            with source_path.open("rb") as source:
                for line in source:
                    if not line.strip():
                        continue
                    if output is None:
                        destination.mkdir(parents=True, exist_ok=True)
                        output = destination / f"part-{number:05d}.jsonl.gz"
                        raw_handle = output.open("xb")
                        gzip_handle = gzip.GzipFile(filename="", mode="wb", compresslevel=6, fileobj=raw_handle, mtime=0)
                    gzip_handle.write(line)
                    rows += 1
                    input_bytes += len(line)
                    if rows % 1000 == 0:
                        gzip_handle.flush()
                        raw_handle.flush()
                        if raw_handle.tell() >= self.shard_size_bytes:
                            close_shard()
        close_shard()
        return shards

    def _shard_record(
        self,
        path: Path,
        root: Path,
        files: Optional[int],
        rows: Optional[int],
        input_bytes: int,
    ) -> Dict[str, Any]:
        result = {
            "path": path.relative_to(root).as_posix(),
            "sha256": _sha256_file(path),
            "compressed_bytes": path.stat().st_size,
            "input_bytes": input_bytes,
        }
        if files is not None:
            result["files"] = files
        if rows is not None:
            result["rows"] = rows
        return result

    def _validate_built_snapshot(self, build_dir: Path, manifest: Mapping[str, Any]) -> None:
        for group in manifest["shards"].values():
            for shards in group.values():
                for shard in shards:
                    path = build_dir / shard["path"]
                    if path.stat().st_size != shard["compressed_bytes"] or _sha256_file(path) != shard["sha256"]:
                        raise ValueError(f"snapshot shard verification failed: {path}")

    def _report_markdown(self, summary: PilotSummary) -> str:
        versions_per_package = summary.package_versions / summary.packages if summary.packages else 0.0
        requirements_per_version = summary.dependency_requirements / summary.package_versions if summary.package_versions else 0.0
        relations_per_graph = summary.dependency_relations / summary.depsdev_successes if summary.depsdev_successes else 0.0
        landing_bytes_per_package = summary.landing_bytes / summary.packages if summary.packages else 0.0
        return f"""# Pilot dataset {summary.dataset_id}

Status: `{summary.status}`. Acceptance criteria met: `{str(summary.criteria_met).lower()}`.

| Metric | Value |
| --- | ---: |
| npm packages | {summary.packages} / {summary.package_target} |
| Full npm package versions | {summary.package_versions} |
| Average versions per package | {versions_per_package:.3f} |
| Dependency requirements | {summary.dependency_requirements} |
| Average requirements per version | {requirements_per_version:.3f} |
| Versions selected for enrichment | {summary.selected_versions} |
| deps.dev successful graphs | {summary.depsdev_successes} |
| deps.dev failures | {summary.depsdev_failures} |
| deps.dev success rate | {summary.depsdev_success_rate:.2%} |
| Normalized dependency relations | {summary.dependency_relations} |
| Average relations per successful graph | {relations_per_graph:.3f} |
| OSV checked versions | {summary.osv_checked} |
| OSV errors | {summary.osv_errors} |
| OSV success rate | {summary.osv_success_rate:.2%} |
| Versions with vulnerabilities | {summary.versions_with_vulnerabilities} |
| Unique OSV records | {summary.unique_osv_records} |
| Normalized vulnerability records | {summary.vulnerability_records} |
| Landing working bytes | {summary.landing_bytes} |
| Landing bytes per package | {landing_bytes_per_package:.3f} |
| Compacted snapshot payload bytes | {summary.snapshot_payload_bytes} |
| Raw / normalized / audit shards | {summary.raw_shards} / {summary.normalized_shards} / {summary.audit_shards} |
| Collection time, seconds | {summary.elapsed_seconds:.3f} |

The npm history is complete for the frozen package list. Enrichment uses at most
`{self.versions_per_package}` evenly distributed historical versions per package (oldest and newest included; `0` means all).
Raw responses are preserved byte-for-byte inside `tar.gz` shards; normalized entities are streamed into
`jsonl.gz` shards. Re-running the same command resumes from validated state markers.
"""

    def _relative_to_data_root(self, path: Path) -> str:
        return path.relative_to(self.data_root).as_posix()
