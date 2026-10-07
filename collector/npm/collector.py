from __future__ import annotations

import concurrent.futures
import dataclasses
import datetime as dt
import hashlib
import json
import logging
import os
import random
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Tuple


SCHEMA_VERSION = "1.0.0"
COLLECTOR_VERSION = "1.0.0"
DEFAULT_QUERY = "keywords:javascript"
DEPENDENCY_TYPES = (
    "dependencies",
    "devDependencies",
    "peerDependencies",
    "optionalDependencies",
)
TIMESTAMP_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})T(?P<time>\d{2}:\d{2}:\d{2})"
    r"(?P<fraction>\.\d{1,9})?(?P<offset>Z|[+-]\d{2}:\d{2})$"
)


LOGGER = logging.getLogger("collector.npm")


class CollectionError(RuntimeError):
    """A package cannot be fetched or normalized."""


class FetchError(CollectionError):
    def __init__(self, url: str, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.url = url
        self.status = status


@dataclasses.dataclass(frozen=True)
class HttpResponse:
    url: str
    status: int
    body: bytes
    headers: Mapping[str, str]


@dataclasses.dataclass(frozen=True)
class PackageResult:
    requested_name: str
    package_name: str
    snapshot_id: str
    raw_path: str
    normalized_paths: Tuple[str, ...]
    versions: int
    requirements: int


@dataclasses.dataclass
class RunSummary:
    run_id: str
    target: int
    downloaded: int = 0
    skipped: int = 0
    failed: int = 0
    available: int = 0
    manifest_path: str = ""
    log_path: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


class JsonEventLog:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def write(self, event: str, **fields: Any) -> None:
        record = {"timestamp": utc_now(), "event": event, **fields}
        encoded = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self._lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(encoded)
                handle.flush()


class HttpClient:
    def __init__(self, timeout: float = 30.0, retries: int = 3):
        self.timeout = timeout
        self.retries = retries
        self.user_agent = f"bigdata-npm-collector/{COLLECTOR_VERSION}"

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
                    body = response.read()
                    return HttpResponse(
                        url=response.geturl(),
                        status=response.status,
                        body=body,
                        headers={key.lower(): value for key, value in response.headers.items()},
                    )
            except urllib.error.HTTPError as exc:
                last_error = exc
                status = exc.code
                retryable = status == 429 or 500 <= status <= 599
                if not retryable or attempt == self.retries:
                    raise FetchError(url, f"HTTP {status} for {url}", status=status) from exc
                delay = _retry_delay(attempt, exc.headers.get("Retry-After"))
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = exc
                if attempt == self.retries:
                    raise FetchError(url, f"request failed for {url}: {exc}") from exc
                delay = _retry_delay(attempt, None)
            time.sleep(delay)

        raise FetchError(url, f"request failed for {url}: {last_error}")


def _retry_delay(attempt: int, retry_after: Optional[str]) -> float:
    if retry_after:
        try:
            return min(60.0, max(0.0, float(retry_after)))
        except ValueError:
            pass
    return min(30.0, (2**attempt) + random.random())


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def normalize_timestamp(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    match = TIMESTAMP_RE.fullmatch(value)
    if match is None:
        return None
    try:
        base = dt.datetime.fromisoformat(f"{match.group('date')}T{match.group('time')}{match.group('offset').replace('Z', '+00:00')}")
    except ValueError:
        return None
    utc = base.astimezone(dt.timezone.utc)
    return utc.strftime("%Y-%m-%dT%H:%M:%S") + (match.group("fraction") or "") + "Z"


def parser_version() -> str:
    source = Path(__file__).read_bytes()
    digest = hashlib.sha256(source).hexdigest()
    return f"npm-collector@{COLLECTOR_VERSION}+source.{digest}"


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


def _json_bytes(value: Any, pretty: bool = False) -> bytes:
    if pretty:
        text = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    else:
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"
    return text.encode("utf-8")


def _jsonl_bytes(records: Iterable[Mapping[str, Any]]) -> bytes:
    return b"".join(_json_bytes(record) for record in records)


def _state_key(package_name: str) -> str:
    return hashlib.sha256(package_name.encode("utf-8")).hexdigest()


def _safe_json_object(body: bytes, context: str) -> Dict[str, Any]:
    try:
        decoded = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CollectionError(f"invalid JSON in {context}: {exc}") from exc
    if not isinstance(decoded, dict):
        raise CollectionError(f"expected a JSON object in {context}")
    return decoded


def normalize_packument(
    packument: Mapping[str, Any],
    provenance: Mapping[str, Any],
) -> Tuple[Dict[str, Any], List[Dict[str, Any]], List[Dict[str, Any]], List[str]]:
    package_name = packument.get("name")
    versions = packument.get("versions")
    if not isinstance(package_name, str) or not package_name:
        raise CollectionError("packument has no non-empty string name")
    if not isinstance(versions, dict) or not versions:
        raise CollectionError(f"packument for {package_name} has no versions object")

    times = packument.get("time") if isinstance(packument.get("time"), dict) else {}
    dist_tags = packument.get("dist-tags") if isinstance(packument.get("dist-tags"), dict) else {}
    common = {"schema_version": SCHEMA_VERSION, "source": "npm", "provenance": dict(provenance)}
    package = {
        **common,
        "package_name": package_name,
        "repository": packument.get("repository"),
        "dist_tags": dist_tags,
        "created_at": normalize_timestamp(times.get("created")),
        "modified_at": normalize_timestamp(times.get("modified")),
    }

    version_records: List[Dict[str, Any]] = []
    requirement_records: List[Dict[str, Any]] = []
    warnings: List[str] = []
    for version, version_document in versions.items():
        if not isinstance(version, str) or not isinstance(version_document, dict):
            warnings.append(f"ignored malformed version entry {version!r}")
            continue
        deprecated_value = version_document.get("deprecated")
        deprecated_message = deprecated_value if isinstance(deprecated_value, str) else None
        version_records.append(
            {
                **common,
                "package_name": package_name,
                "version": version,
                "published_at": normalize_timestamp(times.get(version)),
                "repository": version_document.get("repository"),
                "is_deprecated": bool(deprecated_message),
                "deprecated_message": deprecated_message,
            }
        )
        for dependency_type in DEPENDENCY_TYPES:
            dependencies = version_document.get(dependency_type)
            if dependencies is None:
                continue
            if not isinstance(dependencies, dict):
                warnings.append(f"ignored non-object {dependency_type} in {package_name}@{version}")
                continue
            for dependency_name, requirement in dependencies.items():
                if not isinstance(dependency_name, str) or not isinstance(requirement, str):
                    warnings.append(f"ignored malformed {dependency_type} entry in {package_name}@{version}")
                    continue
                requirement_records.append(
                    {
                        **common,
                        "source_package": package_name,
                        "source_version": version,
                        "dependency_name": dependency_name,
                        "requirement": requirement,
                        "dependency_type": dependency_type,
                    }
                )

    if not version_records:
        raise CollectionError(f"packument for {package_name} has no valid version records")
    return package, version_records, requirement_records, warnings


class NpmCollector:
    def __init__(
        self,
        data_dir: Path,
        registry_url: str = "https://registry.npmjs.org",
        workers: int = 8,
        timeout: float = 30.0,
        retries: int = 3,
        refresh: bool = False,
        client: Optional[HttpClient] = None,
    ):
        self.data_dir = Path(data_dir)
        self.registry_url = registry_url.rstrip("/")
        self.workers = workers
        self.refresh = refresh
        self.client = client or HttpClient(timeout=timeout, retries=retries)
        self.parser_version = parser_version()
        self._event_log: Optional[JsonEventLog] = None

    def run(
        self,
        limit: int,
        query: str = DEFAULT_QUERY,
        package_names: Optional[Sequence[str]] = None,
    ) -> RunSummary:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        run_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid.uuid4().hex[:8]
        log_path = self.data_dir / "logs" / "npm" / f"{run_id}.jsonl"
        manifest_path = self.data_dir / "manifests" / "npm" / f"{run_id}.json"
        self._event_log = JsonEventLog(log_path)
        summary = RunSummary(
            run_id=run_id,
            target=limit,
            log_path=self._relative(log_path),
            manifest_path=self._relative(manifest_path),
        )
        started_at = utc_now()
        results: List[Dict[str, Any]] = []
        self._event_log.write(
            "run_started",
            run_id=run_id,
            target=limit,
            query=query if package_names is None else None,
            explicit_packages=package_names is not None,
            parser_version=self.parser_version,
        )

        if package_names is None:
            candidates = self._search_names(query=query, run_id=run_id)
        else:
            candidates = iter(package_names)

        seen = set()
        exhausted = False
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.workers) as executor:
            while summary.available < summary.target and not exhausted:
                batch: List[str] = []
                while len(batch) < summary.target - summary.available:
                    try:
                        package_name = next(candidates)
                    except StopIteration:
                        exhausted = True
                        break
                    if package_name in seen:
                        continue
                    seen.add(package_name)
                    cached = None if self.refresh else self._complete_state(package_name)
                    if cached is not None:
                        summary.skipped += 1
                        summary.available += 1
                        result = {"package": package_name, "status": "skipped", "snapshot_id": cached.get("snapshot_id")}
                        results.append(result)
                        self._event_log.write("package_skipped", run_id=run_id, **result)
                        if summary.available >= summary.target:
                            break
                    else:
                        batch.append(package_name)

                futures = {executor.submit(self._collect_one, name, run_id): name for name in batch}
                for future in concurrent.futures.as_completed(futures):
                    requested_name = futures[future]
                    try:
                        package_result = future.result()
                    except Exception as exc:  # package isolation is intentional
                        summary.failed += 1
                        result = {
                            "package": requested_name,
                            "status": "failed",
                            "error_type": type(exc).__name__,
                            "error": str(exc),
                            "http_status": getattr(exc, "status", None),
                        }
                        results.append(result)
                        LOGGER.error("failed %s: %s", requested_name, exc)
                        self._event_log.write("package_failed", run_id=run_id, **result)
                    else:
                        summary.downloaded += 1
                        summary.available += 1
                        result = {
                            "package": package_result.package_name,
                            "requested_package": package_result.requested_name,
                            "status": "downloaded",
                            "snapshot_id": package_result.snapshot_id,
                            "versions": package_result.versions,
                            "requirements": package_result.requirements,
                        }
                        results.append(result)
                        if summary.downloaded % 25 == 0 or summary.available == summary.target:
                            LOGGER.info(
                                "npm collection progress: available=%d/%d downloaded=%d failed=%d",
                                summary.available,
                                summary.target,
                                summary.downloaded,
                                summary.failed,
                            )

        finished_at = utc_now()
        manifest = {
            "run_id": run_id,
            "started_at": started_at,
            "finished_at": finished_at,
            "source": "npm",
            "registry_url": self.registry_url,
            "search_endpoint": f"{self.registry_url}/-/v1/search" if package_names is None else None,
            "search_query": query if package_names is None else None,
            "parser_version": self.parser_version,
            "summary": summary.to_dict(),
            "results": results,
        }
        _atomic_write(manifest_path, _json_bytes(manifest, pretty=True))
        self._event_log.write("run_finished", **summary.to_dict())
        return summary

    def _search_names(self, query: str, run_id: str) -> Iterator[str]:
        page_size = 250
        offset = 0
        while True:
            query_string = urllib.parse.urlencode({"text": query, "size": page_size, "from": offset})
            request_url = f"{self.registry_url}/-/v1/search?{query_string}"
            response = self.client.get(request_url)
            retrieved_at = utc_now()
            page_path = (
                self.data_dir
                / "raw"
                / "npm-search"
                / retrieved_at[:10]
                / f"{run_id}-from-{offset}.json"
            )
            _atomic_write(page_path, response.body)
            document = _safe_json_object(response.body, request_url)
            objects = document.get("objects")
            if not isinstance(objects, list):
                raise CollectionError("npm search response has no objects array")
            assert self._event_log is not None
            self._event_log.write(
                "discovery_page",
                run_id=run_id,
                request_url=request_url,
                http_status=response.status,
                raw_path=self._relative(page_path),
                raw_sha256=hashlib.sha256(response.body).hexdigest(),
                result_count=len(objects),
            )
            if not objects:
                return
            for item in objects:
                if not isinstance(item, dict):
                    continue
                package = item.get("package")
                if isinstance(package, dict) and isinstance(package.get("name"), str):
                    yield package["name"]
            offset += len(objects)
            total = document.get("total")
            if isinstance(total, int) and offset >= total:
                return

    def _collect_one(self, requested_name: str, run_id: str) -> PackageResult:
        encoded_name = urllib.parse.quote(requested_name, safe="@")
        request_url = f"{self.registry_url}/{encoded_name}"
        response = self.client.get(request_url)
        retrieved_at = utc_now()
        snapshot_id = str(uuid.uuid4())
        raw_sha256 = hashlib.sha256(response.body).hexdigest()
        date = retrieved_at[:10]
        raw_path = self.data_dir / "raw" / "npm" / date / f"{snapshot_id}.json"
        _atomic_write(raw_path, response.body)

        provenance = {
            "snapshot_id": snapshot_id,
            "request_url": request_url,
            "retrieved_at": retrieved_at,
            "raw_path": self._relative(raw_path),
            "raw_sha256": raw_sha256,
            "parser_version": self.parser_version,
        }
        packument = _safe_json_object(response.body, request_url)
        package, versions, requirements, warnings = normalize_packument(packument, provenance)
        paths = {
            "package": self.data_dir / "normalized" / "v1" / "package" / date / f"{snapshot_id}.jsonl",
            "package_version": self.data_dir
            / "normalized"
            / "v1"
            / "package_version"
            / date
            / f"{snapshot_id}.jsonl",
            "dependency_requirement": self.data_dir
            / "normalized"
            / "v1"
            / "dependency_requirement"
            / date
            / f"{snapshot_id}.jsonl",
        }
        _atomic_write(paths["package"], _jsonl_bytes([package]))
        _atomic_write(paths["package_version"], _jsonl_bytes(versions))
        _atomic_write(paths["dependency_requirement"], _jsonl_bytes(requirements))

        package_name = package["package_name"]
        state = {
            "package_name": package_name,
            "requested_name": requested_name,
            "snapshot_id": snapshot_id,
            "retrieved_at": retrieved_at,
            "request_url": request_url,
            "http_status": response.status,
            "etag": response.headers.get("etag"),
            "raw_path": self._relative(raw_path),
            "raw_sha256": raw_sha256,
            "parser_version": self.parser_version,
            "normalized_paths": [self._relative(path) for path in paths.values()],
            "versions": len(versions),
            "requirements": len(requirements),
        }
        state_path = self._state_path(requested_name)
        _atomic_write(state_path, _json_bytes(state, pretty=True))
        assert self._event_log is not None
        self._event_log.write(
            "package_downloaded",
            run_id=run_id,
            package=package_name,
            requested_package=requested_name,
            snapshot_id=snapshot_id,
            request_url=request_url,
            http_status=response.status,
            raw_path=state["raw_path"],
            raw_sha256=raw_sha256,
            versions=len(versions),
            requirements=len(requirements),
            warnings=warnings,
        )
        return PackageResult(
            requested_name=requested_name,
            package_name=package_name,
            snapshot_id=snapshot_id,
            raw_path=state["raw_path"],
            normalized_paths=tuple(state["normalized_paths"]),
            versions=len(versions),
            requirements=len(requirements),
        )

    def _state_path(self, package_name: str) -> Path:
        return self.data_dir / "state" / "npm" / f"{_state_key(package_name)}.json"

    def _complete_state(self, package_name: str) -> Optional[Dict[str, Any]]:
        state_path = self._state_path(package_name)
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        if (
            not isinstance(state, dict)
            or state.get("package_name") is None
            or state.get("parser_version") != self.parser_version
        ):
            return None
        paths = [state.get("raw_path"), *(state.get("normalized_paths") or [])]
        if not paths or any(not isinstance(path, str) or not (self.data_dir / path).is_file() for path in paths):
            return None
        return state

    def _relative(self, path: Path) -> str:
        return path.relative_to(self.data_dir).as_posix()
