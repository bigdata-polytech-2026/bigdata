from __future__ import annotations

import concurrent.futures
import dataclasses
import datetime as dt
import gzip
import hashlib
import io
import json
import logging
import os
import tarfile
import tempfile
import urllib.parse
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from collector.npm.collector import (
    DEFAULT_QUERY,
    HttpClient,
    HttpResponse,
    _safe_json_object,
    normalize_packument,
    parser_version,
    utc_now,
)


SNAPSHOT_VERSION = "1.0.0"
LOG = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class FetchedPackage:
    requested_name: str
    package_name: str
    response: HttpResponse
    retrieved_at: str
    snapshot_id: str
    versions: int
    requirements: int
    warnings: int


@dataclasses.dataclass
class SnapshotSummary:
    dataset_id: str
    status: str
    package_target: int
    packages: int = 0
    package_versions: int = 0
    dependency_requirements: int = 0
    failed: int = 0
    warnings: int = 0
    batches: int = 0
    raw_bytes: int = 0
    snapshot_bytes: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


def _json_bytes(value: Any, pretty: bool = False) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=pretty,
                       indent=2 if pretty else None,
                       separators=None if pretty else (",", ":")) + "\n").encode()


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def _safe_dataset_id(value: str) -> str:
    if not value or value in {".", ".."} or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for ch in value):
        raise ValueError("invalid dataset id")
    return value


def _tar_add_bytes(archive: tarfile.TarFile, name: str, payload: bytes) -> None:
    info = tarfile.TarInfo(name=name)
    info.size = len(payload)
    info.mtime = 0
    info.mode = 0o644
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    archive.addfile(info, io.BytesIO(payload))


def _tar_writer(path: Path):
    raw = path.open("xb")
    compressed = gzip.GzipFile(filename="", mode="wb", compresslevel=6, fileobj=raw, mtime=0)
    archive = tarfile.open(fileobj=compressed, mode="w|")
    return raw, compressed, archive


def _close_tar(handles) -> None:
    raw, compressed, archive = handles
    archive.close()
    compressed.close()
    raw.close()


class NpmSnapshotCollector:
    """Collect npm packuments into compact, resumable raw/audit shards.

    Unlike the pilot collector, normalized JSONL is never materialized. Each batch
    is validated and compressed before the next batch starts, keeping disk use near
    the final snapshot size.
    """

    def __init__(
        self,
        data_root: Path,
        dataset_id: str,
        package_limit: int = 0,
        batch_size: int = 50,
        workers: int = 8,
        query: str = DEFAULT_QUERY,
        registry_url: str = "https://registry.npmjs.org",
        timeout: float = 30.0,
        retries: int = 3,
        client: Optional[HttpClient] = None,
    ):
        if package_limit < 0 or batch_size < 1 or workers < 1:
            raise ValueError("package_limit must be non-negative; batch_size and workers must be positive")
        self.data_root = Path(data_root)
        self.dataset_id = _safe_dataset_id(dataset_id)
        self.package_limit = package_limit
        self.batch_size = batch_size
        self.workers = workers
        self.query = query
        self.registry_url = registry_url.rstrip("/")
        self.client = client or HttpClient(timeout=timeout, retries=retries)
        self.parser_version = parser_version()
        self.work_dir = self.data_root / "landing" / f".{dataset_id}-npm-snapshot"
        self.build_dir = self.work_dir / "snapshot"
        self.snapshot_dir = self.data_root / "datasets" / dataset_id

    def _config(self) -> Dict[str, Any]:
        return {
            "snapshot_version": SNAPSHOT_VERSION,
            "dataset_id": self.dataset_id,
            "package_limit": self.package_limit,
            "batch_size": self.batch_size,
            "query": self.query,
            "registry_url": self.registry_url,
            "parser_version": self.parser_version,
            "sources": ["npm"],
        }

    def run(self, package_names: Optional[Sequence[str]] = None) -> SnapshotSummary:
        existing = self._existing()
        if existing is not None:
            return existing
        self.build_dir.mkdir(parents=True, exist_ok=True)
        packages_path = self.work_dir / "inputs" / "packages.txt"
        if packages_path.is_file():
            names = [line for line in packages_path.read_text(encoding="utf-8").splitlines() if line]
            self._restore_discovery_shard()
        elif package_names is not None:
            names = list(dict.fromkeys(package_names))
            if self.package_limit:
                names = names[:self.package_limit]
            _atomic_write(packages_path, "".join(f"{name}\n" for name in names).encode())
        else:
            names = self._discover(packages_path)
        if not names:
            raise ValueError("npm discovery returned no packages")
        if self.package_limit and len(names) != self.package_limit:
            raise ValueError(f"discovered {len(names)} packages, expected {self.package_limit}")

        summary = SnapshotSummary(self.dataset_id, "collecting", len(names))
        all_batch_records = []
        batch_count = (len(names) + self.batch_size - 1) // self.batch_size
        LOG.info("npm snapshot %s: %d packages in %d batches", self.dataset_id, len(names), batch_count)
        for index in range(0, len(names), self.batch_size):
            number = index // self.batch_size
            batch_names = names[index:index + self.batch_size]
            record = self._batch(number, batch_names)
            all_batch_records.append(record)
            summary.batches += 1
            summary.packages += record["packages"]
            summary.package_versions += record["versions"]
            summary.dependency_requirements += record["requirements"]
            summary.failed += len(record["failures"])
            summary.warnings += record["warnings"]
            summary.raw_bytes += record["raw"]["input_bytes"]
            LOG.info(
                "npm snapshot %s: batch %d/%d complete, %d/%d packages, %d versions, %d requirements",
                self.dataset_id,
                number + 1,
                batch_count,
                summary.packages,
                summary.package_target,
                summary.package_versions,
                summary.dependency_requirements,
            )

        summary.status = "complete" if summary.failed == 0 and summary.packages == summary.package_target else "incomplete"
        if summary.status != "complete":
            raise RuntimeError(f"snapshot incomplete: {summary.failed} package failures; rerun to retry failed batches")
        inputs = self.build_dir / "inputs"
        inputs.mkdir(parents=True, exist_ok=True)
        _atomic_write(inputs / "packages.txt", packages_path.read_bytes())
        raw_shards = [record["raw"] for record in all_batch_records]
        discovery = self._discovery_record()
        if discovery is not None:
            raw_shards.insert(0, discovery)
        audit_shards = [record["audit"] for record in all_batch_records]
        summary.snapshot_bytes = sum(item["compressed_bytes"] for item in raw_shards + audit_shards)
        manifest = {
            "schema_version": "1.0.0",
            "created_at": utc_now(),
            "immutable": True,
            "config": self._config(),
            "summary": summary.to_dict(),
            "shards": {
                "raw": {"npm": raw_shards, "depsdev": []},
                "audit": {"npm": audit_shards, "depsdev": []},
                "normalized": {"dependency_relation": []},
            },
        }
        _atomic_write(self.build_dir / "manifest.json", _json_bytes(manifest, pretty=True))
        self._validate_snapshot(self.build_dir, manifest)
        self.snapshot_dir.parent.mkdir(parents=True, exist_ok=True)
        os.replace(self.build_dir, self.snapshot_dir)
        LOG.info("npm snapshot %s complete: %.2f GiB compressed", self.dataset_id,
                 summary.snapshot_bytes / (1024 ** 3))
        return summary

    def _existing(self) -> Optional[SnapshotSummary]:
        manifest_path = self.snapshot_dir / "manifest.json"
        if not manifest_path.is_file():
            return None
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("config") != self._config():
            raise ValueError("existing immutable snapshot has different configuration")
        self._validate_snapshot(self.snapshot_dir, manifest)
        return SnapshotSummary(**manifest["summary"])

    def _discover(self, packages_path: Path) -> List[str]:
        names: List[str] = []
        seen = set()
        offset = 0
        page = 0
        discovery_dir = self.work_dir / "discovery"
        discovery_dir.mkdir(parents=True, exist_ok=True)
        while not self.package_limit or len(names) < self.package_limit:
            query = urllib.parse.urlencode({"text": self.query, "size": 250, "from": offset})
            url = f"{self.registry_url}/-/v1/search?{query}"
            response = self.client.get(url)
            _atomic_write(discovery_dir / f"part-{page:05d}.json", response.body)
            document = _safe_json_object(response.body, url)
            objects = document.get("objects")
            if not isinstance(objects, list):
                raise ValueError("npm search response has no objects")
            if not objects:
                break
            for item in objects:
                package = item.get("package") if isinstance(item, dict) else None
                name = package.get("name") if isinstance(package, dict) else None
                if isinstance(name, str) and name not in seen:
                    seen.add(name)
                    names.append(name)
                    if self.package_limit and len(names) == self.package_limit:
                        break
            offset += len(objects)
            page += 1
            total = document.get("total")
            if isinstance(total, int) and offset >= total:
                break
        _atomic_write(packages_path, "".join(f"{name}\n" for name in names).encode())
        self._compact_discovery(discovery_dir)
        return names

    def _compact_discovery(self, directory: Path) -> None:
        destination = self.build_dir / "raw" / "npm" / "part-search.tar.gz"
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.tmp")
        handles = _tar_writer(temporary)
        try:
            for path in sorted(directory.glob("*.json")):
                _tar_add_bytes(handles[2], f"raw/npm-search/{path.name}", path.read_bytes())
        finally:
            _close_tar(handles)
        os.replace(temporary, destination)
        for path in directory.glob("*.json"):
            path.unlink()
        directory.rmdir()

    def _restore_discovery_shard(self) -> None:
        """Finish discovery compaction when a previous run stopped after freezing names."""
        destination = self.build_dir / "raw" / "npm" / "part-search.tar.gz"
        if destination.is_file():
            return
        discovery_dir = self.work_dir / "discovery"
        if discovery_dir.is_dir() and any(discovery_dir.glob("*.json")):
            LOG.info("restoring npm search shard from interrupted discovery")
            self._compact_discovery(discovery_dir)

    def _discovery_record(self) -> Optional[Dict[str, Any]]:
        path = self.build_dir / "raw" / "npm" / "part-search.tar.gz"
        if not path.is_file():
            return None
        with tarfile.open(path, "r:gz") as archive:
            members = [member for member in archive if member.isfile()]
        return self._shard_record(path, len(members), sum(member.size for member in members))

    def _fetch(self, name: str) -> FetchedPackage:
        encoded = urllib.parse.quote(name, safe="@")
        url = f"{self.registry_url}/{encoded}"
        response = self.client.get(url)
        retrieved_at = utc_now()
        snapshot_id = str(uuid.uuid4())
        raw_path = f"raw/npm/{snapshot_id}.json"
        provenance = {
            "snapshot_id": snapshot_id,
            "request_url": url,
            "retrieved_at": retrieved_at,
            "raw_path": raw_path,
            "raw_sha256": hashlib.sha256(response.body).hexdigest(),
            "parser_version": self.parser_version,
        }
        document = _safe_json_object(response.body, url)
        package, versions, requirements, warnings = normalize_packument(document, provenance)
        if package["package_name"] != name:
            raise ValueError(f"npm identity mismatch: requested {name}, received {package['package_name']}")
        return FetchedPackage(name, package["package_name"], response, retrieved_at, snapshot_id,
                              len(versions), len(requirements), len(warnings))

    def _batch(self, number: int, names: Sequence[str]) -> Dict[str, Any]:
        state_path = self.work_dir / "batches" / f"part-{number:05d}.json"
        if state_path.is_file():
            record = json.loads(state_path.read_text(encoding="utf-8"))
            if record.get("names") == list(names) and not record.get("failures"):
                self._verify_record(record)
                return record
        raw_destination = self.build_dir / "raw" / "npm" / f"part-{number:05d}.tar.gz"
        audit_destination = self.build_dir / "audit" / "npm" / f"part-{number:05d}.tar.gz"
        raw_destination.parent.mkdir(parents=True, exist_ok=True)
        audit_destination.parent.mkdir(parents=True, exist_ok=True)
        temporary_root = Path(tempfile.mkdtemp(prefix=f".batch-{number:05d}-", dir=self.work_dir))
        raw_temporary = temporary_root / "raw.tar.gz"
        audit_temporary = temporary_root / "audit.tar.gz"
        fetched: Dict[str, FetchedPackage] = {}
        failures = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.workers) as executor:
            futures = {executor.submit(self._fetch, name): name for name in names}
            for future in concurrent.futures.as_completed(futures):
                name = futures[future]
                try:
                    fetched[name] = future.result()
                except Exception as exc:
                    failures.append({"package": name, "error_type": type(exc).__name__, "error": str(exc)})
        raw_handles = _tar_writer(raw_temporary)
        audit_handles = _tar_writer(audit_temporary)
        versions = requirements = warnings = raw_bytes = 0
        try:
            for name in names:
                item = fetched.get(name)
                if item is None:
                    continue
                raw_path = f"raw/npm/{item.snapshot_id}.json"
                raw_sha = hashlib.sha256(item.response.body).hexdigest()
                _tar_add_bytes(raw_handles[2], raw_path, item.response.body)
                state = {
                    "package_name": item.package_name,
                    "requested_name": item.requested_name,
                    "snapshot_id": item.snapshot_id,
                    "retrieved_at": item.retrieved_at,
                    "request_url": item.response.url,
                    "http_status": item.response.status,
                    "etag": item.response.headers.get("etag"),
                    "raw_path": raw_path,
                    "raw_sha256": raw_sha,
                    "parser_version": self.parser_version,
                    "versions": item.versions,
                    "requirements": item.requirements,
                }
                _tar_add_bytes(audit_handles[2], f"state/npm/{item.snapshot_id}.json", _json_bytes(state, pretty=True))
                versions += item.versions
                requirements += item.requirements
                warnings += item.warnings
                raw_bytes += len(item.response.body)
        finally:
            _close_tar(raw_handles)
            _close_tar(audit_handles)
        os.replace(raw_temporary, raw_destination)
        os.replace(audit_temporary, audit_destination)
        temporary_root.rmdir()
        record = {
            "number": number,
            "names": list(names),
            "packages": len(fetched),
            "versions": versions,
            "requirements": requirements,
            "warnings": warnings,
            "failures": sorted(failures, key=lambda value: value["package"]),
            "raw": self._shard_record(raw_destination, len(fetched), raw_bytes),
            "audit": self._shard_record(audit_destination, len(fetched), None),
        }
        _atomic_write(state_path, _json_bytes(record, pretty=True))
        if failures:
            # A failed batch is deliberately retried in full on the next run.
            raise RuntimeError(f"batch {number} failed for {len(failures)} packages")
        return record

    def _shard_record(self, path: Path, files: int, input_bytes: Optional[int]) -> Dict[str, Any]:
        result = {
            "path": path.relative_to(self.build_dir).as_posix(),
            "sha256": _digest(path),
            "compressed_bytes": path.stat().st_size,
            "files": files,
        }
        if input_bytes is not None:
            result["input_bytes"] = input_bytes
        return result

    def _verify_record(self, record: Mapping[str, Any]) -> None:
        for key in ("raw", "audit"):
            shard = record[key]
            path = self.build_dir / shard["path"]
            if not path.is_file() or path.stat().st_size != shard["compressed_bytes"] or _digest(path) != shard["sha256"]:
                raise ValueError(f"corrupt batch shard: {path}")

    def _validate_snapshot(self, root: Path, manifest: Mapping[str, Any]) -> None:
        for group in ("raw", "audit"):
            for shards in manifest["shards"][group].values():
                for shard in shards:
                    path = root / shard["path"]
                    if not path.is_file() or path.stat().st_size != shard["compressed_bytes"] or _digest(path) != shard["sha256"]:
                        raise ValueError(f"snapshot shard verification failed: {path}")

