from __future__ import annotations

import concurrent.futures
import collections
import dataclasses
import datetime as dt
import gzip
import hashlib
import io
import json
import logging
import os
import string
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from collector.npm.collector import (
    DEFAULT_QUERY,
    FetchError,
    HttpClient,
    HttpResponse,
    _safe_json_object,
    _retry_delay,
    normalize_packument,
    parser_version,
    utc_now,
)


SNAPSHOT_VERSION = "1.0.0"
MAX_PACKAGE_ATTEMPTS = 5
REGISTRY_INDEX_URL = "https://replicate.npmjs.com/registry/_all_docs"
LOG = logging.getLogger(__name__)


class SnapshotHttpClient(HttpClient):
    """HTTP client with an absolute per-attempt deadline for pathological packuments."""

    def __init__(self, timeout: float, retries: int):
        super().__init__(timeout=timeout, retries=retries)
        self.total_timeout = max(20.0, timeout * 2)

    def get(self, url: str) -> HttpResponse:
        last_error: Optional[BaseException] = None
        for attempt in range(self.retries + 1):
            request = urllib.request.Request(
                url,
                headers={"Accept": "application/json", "User-Agent": self.user_agent},
                method="GET",
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    deadline = time.monotonic() + self.total_timeout
                    chunks = []
                    while True:
                        # read1 returns currently available decrypted bytes instead of
                        # waiting to fill the requested buffer, so trickle responses
                        # cannot evade the absolute deadline.
                        block = response.read1(64 * 1024)
                        if not block:
                            break
                        chunks.append(block)
                        if time.monotonic() > deadline:
                            raise TimeoutError(f"absolute download deadline exceeded for {url}")
                    return HttpResponse(
                        url=response.geturl(), status=response.status, body=b"".join(chunks),
                        headers={key.lower(): value for key, value in response.headers.items()},
                    )
            except urllib.error.HTTPError as exc:
                last_error = exc
                retryable = exc.code == 429 or 500 <= exc.code <= 599
                if not retryable or attempt == self.retries:
                    raise FetchError(url, f"HTTP {exc.code} for {url}", status=exc.code) from exc
                delay = _retry_delay(attempt, exc.headers.get("Retry-After"))
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = exc
                if attempt == self.retries:
                    raise FetchError(url, f"request failed for {url}: {exc}") from exc
                delay = _retry_delay(attempt, None)
            time.sleep(delay)
        raise FetchError(url, f"request failed for {url}: {last_error}")


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
    dependency_names: Tuple[str, ...]


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
    # Registry JSON compresses well even at level 1; the lower CPU cost matters for
    # tens of thousands of packuments and keeps the collector network-bound.
    compressed = gzip.GzipFile(filename="", mode="wb", compresslevel=1, fileobj=raw, mtime=0)
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
        expand_dependencies: bool = False,
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
        self.expand_dependencies = expand_dependencies
        self.client = client or SnapshotHttpClient(timeout=timeout, retries=retries)
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
            "expand_dependencies": self.expand_dependencies,
            "registry_index_url": REGISTRY_INDEX_URL if self.expand_dependencies else None,
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
        if self.package_limit and not self.expand_dependencies and len(names) != self.package_limit:
            raise ValueError(f"discovered {len(names)} packages, expected {self.package_limit}")

        target = self.package_limit if self.expand_dependencies and self.package_limit else len(names)
        summary = SnapshotSummary(self.dataset_id, "collecting", target)
        all_batch_records = []
        seen = set(names)
        attempts = collections.Counter(names)
        existing_states = sorted((self.work_dir / "batches").glob("part-*.json"))
        known_successes = set()
        if self.expand_dependencies:
            for path in existing_states:
                record = json.loads(path.read_text(encoding="utf-8"))
                failed_names = {item["package"] for item in record.get("failures", [])}
                known_successes.update(name for name in record.get("names", []) if name not in failed_names)
        completed = set()
        counted_successes = set()
        index = 0
        LOG.info("npm snapshot %s: %d seed packages, target %d successful packages",
                 self.dataset_id, len(names), target)
        while summary.packages < target:
            if index >= len(names):
                additions = self._fill_from_registry_index(seen, target - summary.packages)
                if not additions:
                    break
                seen.update(additions)
                names.extend(additions)
                attempts.update(additions)
                _atomic_write(packages_path, "".join(f"{name}\n" for name in names).encode())
                self._compact_registry_index()
            number = len(all_batch_records)
            remaining = target - summary.packages
            limit = min(self.batch_size, remaining)
            if number < len(existing_states):
                saved = json.loads(existing_states[number].read_text(encoding="utf-8"))
                batch_names = []
                for expected in saved["names"]:
                    while index < len(names) and names[index] != expected:
                        if names[index] not in completed:
                            raise ValueError(f"saved batch {number} no longer matches the frozen package queue")
                        index += 1
                    if index == len(names):
                        raise ValueError(f"saved batch {number} is missing from the frozen package queue")
                    batch_names.append(names[index])
                    index += 1
            else:
                batch_names = []
                while index < len(names) and len(batch_names) < limit:
                    name = names[index]
                    index += 1
                    if not self.expand_dependencies or name not in completed:
                        batch_names.append(name)
                if not batch_names:
                    continue
            record = self._batch(number, batch_names)
            all_batch_records.append(record)
            failed_names = {item["package"] for item in record["failures"]}
            successful_names = set(record["names"]) - failed_names
            new_successes = successful_names - counted_successes
            counted_successes.update(successful_names)
            completed.update(successful_names)
            summary.batches += 1
            summary.packages += len(new_successes)
            summary.package_versions += record["versions"]
            summary.dependency_requirements += record["requirements"]
            summary.failed += len(record["failures"])
            summary.warnings += record["warnings"]
            summary.raw_bytes += record["raw"]["input_bytes"]
            if self.expand_dependencies:
                additions = [name for name in record["discovered_dependencies"] if name not in seen]
                seen.update(additions)
                names.extend(additions)
                attempts.update(additions)
                retries = []
                for failure in record["failures"]:
                    name = failure["package"]
                    if (not failure["terminal"] and name not in completed and name not in known_successes
                            and attempts[name] < MAX_PACKAGE_ATTEMPTS):
                        retries.append(name)
                        attempts[name] += 1
                names.extend(retries)
                _atomic_write(packages_path, "".join(f"{name}\n" for name in names).encode())
            LOG.info(
                "npm snapshot %s: batch %d complete, %d/%d packages, queue=%d, %d versions, %d requirements",
                self.dataset_id,
                number + 1,
                summary.packages,
                summary.package_target,
                len(names) - index,
                summary.package_versions,
                summary.dependency_requirements,
            )

        summary.status = "complete" if summary.packages >= summary.package_target else "incomplete"
        if summary.status != "complete":
            raise RuntimeError(f"snapshot incomplete: {summary.failed} package failures; rerun to retry failed batches")
        inputs = self.build_dir / "inputs"
        inputs.mkdir(parents=True, exist_ok=True)
        _atomic_write(inputs / "packages.txt", packages_path.read_bytes())
        failures = [failure for record in all_batch_records for failure in record["failures"]]
        _atomic_write(inputs / "failures.json", _json_bytes(failures, pretty=True))
        raw_shards = [record["raw"] for record in all_batch_records]
        discovery = self._discovery_record()
        if discovery is not None:
            raw_shards.insert(0, discovery)
        registry_index = self._registry_index_record()
        if registry_index is not None:
            raw_shards.insert(1 if discovery is not None else 0, registry_index)
        audit_shards = [record["audit"] for record in all_batch_records]
        summary.snapshot_bytes = sum(item["compressed_bytes"] for item in raw_shards + audit_shards)
        manifest = {
            "schema_version": "1.0.0",
            "created_at": utc_now(),
            "immutable": True,
            "config": self._config(),
            "summary": summary.to_dict(),
            "inputs": {
                "packages": self._input_record(inputs / "packages.txt"),
                "failures": self._input_record(inputs / "failures.json"),
            },
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
        discovery_dir = self.work_dir / "discovery"
        discovery_dir.mkdir(parents=True, exist_ok=True)
        first = self._search_page(discovery_dir, 0, 0)
        objects = first.get("objects")
        if not isinstance(objects, list):
            raise ValueError("npm search response has no objects")
        total = first.get("total")
        if not isinstance(total, int) or total < 0:
            raise ValueError("npm search response has no valid total")
        target = min(total, self.package_limit) if self.package_limit else total
        page_count = (target + 249) // 250
        pages: Dict[int, Mapping[str, Any]] = {0: first}
        LOG.info("npm discovery: %d search results across %d pages", target, page_count)
        if page_count > 1:
            with concurrent.futures.ThreadPoolExecutor(max_workers=self.workers) as executor:
                futures = {
                    executor.submit(self._search_page, discovery_dir, page, page * 250): page
                    for page in range(1, page_count)
                }
                completed = 1
                for future in concurrent.futures.as_completed(futures):
                    page = futures[future]
                    pages[page] = future.result()
                    completed += 1
                    if completed % 20 == 0 or completed == page_count:
                        LOG.info("npm discovery: %d/%d pages", completed, page_count)

        names: List[str] = []
        seen = set()
        for page in range(page_count):
            document = pages[page]
            objects = document.get("objects")
            if not isinstance(objects, list):
                raise ValueError("npm search response has no objects")
            for item in objects:
                package = item.get("package") if isinstance(item, dict) else None
                name = package.get("name") if isinstance(package, dict) else None
                if isinstance(name, str) and name not in seen:
                    seen.add(name)
                    names.append(name)
                    if self.package_limit and len(names) == self.package_limit:
                        break
            if self.package_limit and len(names) == self.package_limit:
                break
        _atomic_write(packages_path, "".join(f"{name}\n" for name in names).encode())
        self._compact_discovery(discovery_dir)
        return names

    def _search_page(self, directory: Path, page: int, offset: int) -> Mapping[str, Any]:
        cached = directory / f"part-{page:05d}.json"
        if cached.is_file():
            return _safe_json_object(cached.read_bytes(), cached.as_posix())
        query = urllib.parse.urlencode({"text": self.query, "size": 250, "from": offset})
        url = f"{self.registry_url}/-/v1/search?{query}"
        response = self.client.get(url)
        _atomic_write(cached, response.body)
        return _safe_json_object(response.body, url)

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

    def _fill_from_registry_index(self, seen: set[str], needed: int) -> List[str]:
        destination = self.build_dir / "raw" / "npm" / "part-registry-index.tar.gz"
        if destination.is_file():
            return []
        directory = self.work_dir / "registry-index"
        directory.mkdir(parents=True, exist_ok=True)
        prefixes = [f"@{value}" for value in string.ascii_lowercase]
        prefixes += list(string.ascii_lowercase + string.digits)
        pages: Dict[int, Mapping[str, Any]] = {}
        LOG.info("npm registry index: collecting %d prefix strata", len(prefixes))
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(self.workers, 8)) as executor:
            futures = {
                executor.submit(self._registry_index_page, directory, number, prefix): number
                for number, prefix in enumerate(prefixes)
            }
            for future in concurrent.futures.as_completed(futures):
                number = futures[future]
                pages[number] = future.result()
        additions = []
        desired = max(needed * 2, 20_000)
        for number in range(len(prefixes)):
            rows = pages[number].get("rows")
            if not isinstance(rows, list):
                raise ValueError("npm registry index response has no rows")
            for row in rows:
                name = row.get("id") if isinstance(row, dict) else None
                if isinstance(name, str) and not name.startswith("_design/") and name not in seen:
                    additions.append(name)
                    seen.add(name)
                    if len(additions) >= desired:
                        LOG.info("npm registry index: selected %d additional packages", len(additions))
                        return additions
        LOG.info("npm registry index: selected %d additional packages", len(additions))
        return additions

    def _registry_index_page(self, directory: Path, number: int, prefix: str) -> Mapping[str, Any]:
        cached = directory / f"part-{number:05d}.json"
        if cached.is_file():
            return _safe_json_object(cached.read_bytes(), cached.as_posix())
        query = urllib.parse.urlencode({
            "limit": 1000,
            "startkey": json.dumps(prefix),
            "endkey": json.dumps(prefix + "\ufff0"),
        })
        url = f"{REGISTRY_INDEX_URL}?{query}"
        response = self.client.get(url)
        _atomic_write(cached, response.body)
        return _safe_json_object(response.body, url)

    def _compact_registry_index(self) -> None:
        directory = self.work_dir / "registry-index"
        if not directory.is_dir():
            return
        destination = self.build_dir / "raw" / "npm" / "part-registry-index.tar.gz"
        temporary = destination.with_name(f".{destination.name}.tmp")
        handles = _tar_writer(temporary)
        try:
            for path in sorted(directory.glob("*.json")):
                _tar_add_bytes(handles[2], f"raw/npm-registry-index/{path.name}", path.read_bytes())
        finally:
            _close_tar(handles)
        os.replace(temporary, destination)
        for path in directory.glob("*.json"):
            path.unlink()
        directory.rmdir()

    def _registry_index_record(self) -> Optional[Dict[str, Any]]:
        path = self.build_dir / "raw" / "npm" / "part-registry-index.tar.gz"
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
                              len(versions), len(requirements), len(warnings),
                              tuple(sorted({item["dependency_name"] for item in requirements
                                            if item["dependency_type"] == "dependencies"})))

    def _batch(self, number: int, names: Sequence[str]) -> Dict[str, Any]:
        state_path = self.work_dir / "batches" / f"part-{number:05d}.json"
        if state_path.is_file():
            record = json.loads(state_path.read_text(encoding="utf-8"))
            reusable_failures = self.expand_dependencies or all(item.get("terminal") for item in record.get("failures", []))
            has_expansion = not self.expand_dependencies or "discovered_dependencies" in record
            if record.get("names") == list(names) and reusable_failures and has_expansion:
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
                    terminal = isinstance(exc, FetchError) and exc.status in {403, 404, 410}
                    failures.append({"package": name, "error_type": type(exc).__name__,
                                     "error": str(exc), "terminal": terminal})
        raw_handles = _tar_writer(raw_temporary)
        audit_handles = _tar_writer(audit_temporary)
        versions = requirements = warnings = raw_bytes = 0
        discovered_dependencies = set()
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
                discovered_dependencies.update(item.dependency_names)
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
            "discovered_dependencies": sorted(discovered_dependencies),
            "raw": self._shard_record(raw_destination, len(fetched), raw_bytes),
            "audit": self._shard_record(audit_destination, len(fetched), None),
        }
        _atomic_write(state_path, _json_bytes(record, pretty=True))
        if failures and not self.expand_dependencies:
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

    def _input_record(self, path: Path) -> Dict[str, Any]:
        return {"path": path.relative_to(self.build_dir).as_posix(), "sha256": _digest(path),
                "bytes": path.stat().st_size}

    def _verify_record(self, record: Mapping[str, Any]) -> None:
        for key in ("raw", "audit"):
            shard = record[key]
            path = self.build_dir / shard["path"]
            if not path.is_file() or path.stat().st_size != shard["compressed_bytes"] or _digest(path) != shard["sha256"]:
                raise ValueError(f"corrupt batch shard: {path}")

    def _validate_snapshot(self, root: Path, manifest: Mapping[str, Any]) -> None:
        for record in manifest.get("inputs", {}).values():
            path = root / record["path"]
            if not path.is_file() or path.stat().st_size != record["bytes"] or _digest(path) != record["sha256"]:
                raise ValueError(f"snapshot input verification failed: {path}")
        for group in ("raw", "audit"):
            for shards in manifest["shards"][group].values():
                for shard in shards:
                    path = root / shard["path"]
                    if not path.is_file() or path.stat().st_size != shard["compressed_bytes"] or _digest(path) != shard["sha256"]:
                        raise ValueError(f"snapshot shard verification failed: {path}")

