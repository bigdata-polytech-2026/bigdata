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
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


SCHEMA_VERSION = "2.0.0"
COLLECTOR_VERSION = "1.0.0"
OSV_QUERY_PATH = "/v1/query"
TIMESTAMP_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})T(?P<time>\d{2}:\d{2}:\d{2})"
    r"(?P<fraction>\.\d{1,9})?(?P<offset>Z|[+-]\d{2}:\d{2})$"
)
LOGGER = logging.getLogger("collector.osv")


class CollectionError(RuntimeError):
    """An OSV response cannot be used to create normalized records."""


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


@dataclasses.dataclass
class RunSummary:
    run_id: str
    target: int
    checked: int = 0
    downloaded: int = 0
    skipped: int = 0
    versions_with_vulnerabilities: int = 0
    unique_osv_records: int = 0
    vulnerability_records: int = 0
    api_errors: int = 0
    elapsed_seconds: float = 0.0
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
        line = json.dumps({"timestamp": utc_now(), "event": event, **fields}, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self._lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line)


class HttpClient:
    def __init__(self, timeout: float = 30.0, retries: int = 3):
        self.timeout = timeout
        self.retries = retries

    def post_json(self, url: str, value: Mapping[str, Any]) -> HttpResponse:
        payload = json.dumps(value, separators=(",", ":")).encode("utf-8")
        for attempt in range(self.retries + 1):
            request = urllib.request.Request(
                url, data=payload,
                headers={"Accept": "application/json", "Content-Type": "application/json", "User-Agent": f"bigdata-osv-collector/{COLLECTOR_VERSION}"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    return HttpResponse(response.geturl(), response.status, response.read(), {key.lower(): value for key, value in response.headers.items()})
            except urllib.error.HTTPError as exc:
                retryable = exc.code == 429 or 500 <= exc.code <= 599
                if not retryable or attempt == self.retries:
                    raise FetchError(url, f"HTTP {exc.code} for {url}", status=exc.code) from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                if attempt == self.retries:
                    raise FetchError(url, f"request failed for {url}: {exc}") from exc
            time.sleep(min(30.0, 2**attempt + random.random()))
        raise AssertionError("unreachable")


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def normalize_timestamp(value: Any) -> Optional[str]:
    if not isinstance(value, str) or TIMESTAMP_RE.fullmatch(value) is None:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") + (TIMESTAMP_RE.fullmatch(value).group("fraction") or "") + "Z"


def parser_version() -> str:
    return f"osv-collector@{COLLECTOR_VERSION}+source.{hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}"


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
    return (json.dumps(value, ensure_ascii=False, indent=2 if pretty else None, sort_keys=pretty, separators=None if pretty else (",", ":")) + "\n").encode("utf-8")


def _state_key(package_name: str, version: str) -> str:
    return hashlib.sha256(f"{package_name}\0{version}".encode("utf-8")).hexdigest()


def _object(value: Any, name: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise CollectionError(f"expected JSON object in {name}")
    return value


def _severity(values: Any) -> List[Dict[str, Optional[str]]]:
    result = []
    if not isinstance(values, list):
        return result
    for item in values:
        if isinstance(item, dict) and isinstance(item.get("type"), str) and isinstance(item.get("score"), str):
            result.append({"type": item["type"], "score": item["score"], "source": item.get("source") if isinstance(item.get("source"), str) else None})
    return result


def normalize_advisory(advisory: Mapping[str, Any], provenance: Mapping[str, Any]) -> Tuple[List[Dict[str, Any]], List[str]]:
    osv_id = advisory.get("id")
    modified_at = normalize_timestamp(advisory.get("modified"))
    if not isinstance(osv_id, str) or not osv_id or modified_at is None:
        raise CollectionError("OSV advisory has no valid id or modified timestamp")
    top_severity = _severity(advisory.get("severity"))
    aliases = [value for value in advisory.get("aliases", []) if isinstance(value, str)] if isinstance(advisory.get("aliases"), list) else []
    affected = advisory.get("affected")
    if not isinstance(affected, list):
        return [], [f"{osv_id}: advisory has no affected array"]
    has_package_severity = any(isinstance(item, dict) and isinstance(item.get("severity"), list) for item in affected)
    records, warnings = [], []
    for index, item in enumerate(affected):
        if not isinstance(item, dict):
            warnings.append(f"{osv_id}: ignored malformed affected[{index}]")
            continue
        package = item.get("package")
        if not isinstance(package, dict) or package.get("ecosystem") != "npm" or not isinstance(package.get("name"), str) or not package["name"]:
            continue
        versions = item.get("versions") if isinstance(item.get("versions"), list) else []
        versions = [value for value in versions if isinstance(value, str)]
        ranges = item.get("ranges") if isinstance(item.get("ranges"), list) else []
        clean_ranges, fixed_versions = [], []
        for range_value in ranges:
            if not isinstance(range_value, dict) or not isinstance(range_value.get("type"), str) or not isinstance(range_value.get("events"), list):
                continue
            events = []
            for event in range_value["events"]:
                if not isinstance(event, dict):
                    continue
                keys = [key for key in ("introduced", "fixed", "last_affected", "limit") if isinstance(event.get(key), str)]
                if len(keys) == 1:
                    events.append({keys[0]: event[keys[0]]})
                    if keys[0] == "fixed" and range_value["type"] in ("SEMVER", "ECOSYSTEM") and event[keys[0]] not in fixed_versions:
                        fixed_versions.append(event[keys[0]])
            if events:
                clean_ranges.append({"type": range_value["type"], "repo": range_value.get("repo") if isinstance(range_value.get("repo"), str) else None, "events": events, "database_specific": range_value.get("database_specific") if isinstance(range_value.get("database_specific"), dict) else None})
        if not versions and not clean_ranges:
            warnings.append(f"{osv_id}: npm affected[{index}] has no usable versions or ranges")
            continue
        item_severity = _severity(item.get("severity"))
        records.append({
            "schema_version": SCHEMA_VERSION, "source": "osv", "provenance": dict(provenance),
            "osv_id": osv_id, "affected_index": index, "package_name": package["name"], "ecosystem": "npm",
            "package_purl": package.get("purl") if isinstance(package.get("purl"), str) else None,
            "aliases": aliases,
            "affected": {"versions": versions, "ranges": clean_ranges, "ecosystem_specific": item.get("ecosystem_specific") if isinstance(item.get("ecosystem_specific"), dict) else None, "database_specific": item.get("database_specific") if isinstance(item.get("database_specific"), dict) else None},
            "fixed_versions": fixed_versions, "published_at": normalize_timestamp(advisory.get("published")),
            "modified_at": modified_at, "severity": item_severity if item_severity else ([] if has_package_severity else top_severity),
            "withdrawn_at": normalize_timestamp(advisory.get("withdrawn")),
        })
    return records, warnings


class OsvCollector:
    def __init__(
        self,
        data_dir: Path,
        api_url: str = "https://api.osv.dev",
        workers: int = 8,
        timeout: float = 30.0,
        retries: int = 3,
        refresh: bool = False,
        client: Optional[HttpClient] = None,
    ):
        self.data_dir = Path(data_dir)
        self.api_url = api_url.rstrip("/")
        self.workers = workers
        self.refresh = refresh
        self.client = client or HttpClient(timeout, retries)
        self.parser_version = parser_version()
        self._event_log: Optional[JsonEventLog] = None

    def run(self, package_versions: Sequence[Tuple[str, str]]) -> RunSummary:
        requested = list(dict.fromkeys(package_versions))
        if not requested:
            raise ValueError("at least one package/version is required")
        if any(not package_name or not version for package_name, version in requested):
            raise ValueError("package names and versions must be non-empty")
        run_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid.uuid4().hex[:8]
        log_path, manifest_path = self.data_dir / "logs" / "osv" / f"{run_id}.jsonl", self.data_dir / "manifests" / "osv" / f"{run_id}.json"
        self._event_log = JsonEventLog(log_path)
        summary = RunSummary(run_id=run_id, target=len(requested), manifest_path=self._relative(manifest_path), log_path=self._relative(log_path))
        started, started_at = time.monotonic(), utc_now()
        results_by_index: Dict[int, Dict[str, Any]] = {}
        unique_ids = set()
        pending = []
        self._event_log.write("run_started", run_id=run_id, target=summary.target, workers=self.workers, parser_version=self.parser_version)

        for index, (package_name, version) in enumerate(requested):
            state = None if self.refresh else self._complete_state(package_name, version)
            if state is None:
                pending.append((index, package_name, version))
                continue
            result = {**state, "package_name": package_name, "version": version, "result_status": state["status"], "status": "skipped"}
            results_by_index[index] = result
            self._apply_success(summary, result, unique_ids, skipped=True)
            self._event_log.write("query_skipped", run_id=run_id, **result)

        with concurrent.futures.ThreadPoolExecutor(max_workers=self.workers) as executor:
            futures = {
                executor.submit(self._collect_one, package_name, version, run_id): (index, package_name, version)
                for index, package_name, version in pending
            }
            for future in concurrent.futures.as_completed(futures):
                index, package_name, version = futures[future]
                try:
                    result, ids = future.result()
                except Exception as exc:  # query isolation is intentional
                    summary.api_errors += 1
                    result = {"package_name": package_name, "version": version, "status": "api_error", "error_type": type(exc).__name__, "error": str(exc), "http_status": getattr(exc, "status", None)}
                    self._event_log.write("query_failed", run_id=run_id, **result)
                    LOGGER.error("OSV query failed for %s@%s: %s", package_name, version, exc)
                else:
                    result["osv_ids"] = sorted(ids)
                    self._apply_success(summary, result, unique_ids, skipped=False)
                results_by_index[index] = result
                processed = summary.checked + summary.api_errors
                if processed % 25 == 0 or processed == summary.target:
                    LOGGER.info("OSV progress: processed=%d/%d checked=%d failed=%d", processed, summary.target, summary.checked, summary.api_errors)

        summary.unique_osv_records = len(unique_ids)
        summary.elapsed_seconds = round(time.monotonic() - started, 3)
        manifest = {"run_id": run_id, "started_at": started_at, "finished_at": utc_now(), "source": "osv", "api_url": self.api_url, "parser_version": self.parser_version, "summary": summary.to_dict(), "results": [results_by_index[index] for index in range(len(requested))]}
        _atomic_write(manifest_path, _json_bytes(manifest, pretty=True)); self._event_log.write("run_finished", **summary.to_dict())
        return summary

    def _apply_success(self, summary: RunSummary, result: Mapping[str, Any], unique_ids: set, skipped: bool) -> None:
        summary.checked += 1
        if skipped:
            summary.skipped += 1
        else:
            summary.downloaded += 1
        result_status = result.get("result_status", result.get("status"))
        if result_status == "vulnerabilities_found":
            summary.versions_with_vulnerabilities += 1
        summary.vulnerability_records += int(result.get("vulnerability_records", 0))
        ids = result.get("osv_ids")
        if isinstance(ids, list):
            unique_ids.update(value for value in ids if isinstance(value, str))

    def _collect_one(self, package_name: str, version: str, run_id: str) -> Tuple[Dict[str, Any], set]:
        url = self.api_url + OSV_QUERY_PATH
        base_query = {"package": {"name": package_name, "ecosystem": "npm"}, "version": version}
        records, warnings, ids, raw_pages = [], [], set(), []
        next_page_token = None
        seen_page_tokens = set()
        first_snapshot_id = None
        first_retrieved_at = None
        response_status = None

        while True:
            query = dict(base_query)
            if next_page_token is not None:
                query["page_token"] = next_page_token
            response = self.client.post_json(url, query)
            response_status = response.status
            retrieved_at, snapshot_id = utc_now(), str(uuid.uuid4())
            first_snapshot_id = first_snapshot_id or snapshot_id
            first_retrieved_at = first_retrieved_at or retrieved_at
            raw_path = self.data_dir / "raw" / "osv" / retrieved_at[:10] / f"{snapshot_id}.json"
            _atomic_write(raw_path, response.body)
            raw_sha256 = hashlib.sha256(response.body).hexdigest()
            raw_pages.append({"snapshot_id": snapshot_id, "retrieved_at": retrieved_at, "raw_path": self._relative(raw_path), "raw_sha256": raw_sha256, "http_status": response.status})
            document = _object(json.loads(response.body.decode("utf-8")), url)
            advisories = document.get("vulns", [])
            if not isinstance(advisories, list):
                raise CollectionError("OSV query response has non-list vulns")
            provenance = {"snapshot_id": snapshot_id, "request_url": url, "retrieved_at": retrieved_at, "raw_path": self._relative(raw_path), "raw_sha256": raw_sha256, "parser_version": self.parser_version}
            for advisory in advisories:
                advisory_records, advisory_warnings = normalize_advisory(_object(advisory, "OSV vuln"), provenance)
                records.extend(advisory_records)
                warnings.extend(advisory_warnings)
                if isinstance(advisory.get("id"), str):
                    ids.add(advisory["id"])
            token = document.get("next_page_token")
            if not isinstance(token, str) or not token:
                break
            if token in seen_page_tokens:
                raise CollectionError("OSV query repeated next_page_token")
            seen_page_tokens.add(token)
            next_page_token = token

        assert first_snapshot_id is not None and first_retrieved_at is not None and response_status is not None
        normalized_path = self.data_dir / "normalized" / "v2" / "vulnerability" / first_retrieved_at[:10] / f"{first_snapshot_id}.jsonl"
        _atomic_write(normalized_path, b"".join(_json_bytes(record) for record in records))
        status = "vulnerabilities_found" if ids else "no_vulnerabilities"
        result = {
            "package_name": package_name,
            "version": version,
            "status": status,
            "snapshot_id": first_snapshot_id,
            "request_url": url,
            "retrieved_at": first_retrieved_at,
            "http_status": response_status,
            "raw_path": raw_pages[0]["raw_path"],
            "raw_sha256": raw_pages[0]["raw_sha256"],
            "raw_pages": raw_pages,
            "pages": len(raw_pages),
            "raw_bytes": sum((self.data_dir / page["raw_path"]).stat().st_size for page in raw_pages),
            "normalized_path": self._relative(normalized_path),
            "osv_records": len(ids),
            "vulnerability_records": len(records),
            "osv_ids": sorted(ids),
        }
        state = {**result, "parser_version": self.parser_version}
        _atomic_write(self._state_path(package_name, version), _json_bytes(state, pretty=True))
        assert self._event_log is not None
        self._event_log.write("query_completed", run_id=run_id, **result, warnings=warnings)
        return result, ids

    def _state_path(self, package_name: str, version: str) -> Path:
        return self.data_dir / "state" / "osv" / f"{_state_key(package_name, version)}.json"

    def _complete_state(self, package_name: str, version: str) -> Optional[Dict[str, Any]]:
        try:
            state = json.loads(self._state_path(package_name, version).read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        if (
            not isinstance(state, dict)
            or state.get("package_name") != package_name
            or state.get("version") != version
            or state.get("status") not in {"vulnerabilities_found", "no_vulnerabilities"}
            or state.get("parser_version") != self.parser_version
        ):
            return None
        normalized_path = state.get("normalized_path")
        if not isinstance(normalized_path, str) or not (self.data_dir / normalized_path).is_file():
            return None
        pages = state.get("raw_pages")
        if not isinstance(pages, list) or not pages:
            pages = [{"raw_path": state.get("raw_path"), "raw_sha256": state.get("raw_sha256")}]
        for page in pages:
            if not isinstance(page, dict) or not isinstance(page.get("raw_path"), str):
                return None
            raw_path = self.data_dir / page["raw_path"]
            try:
                if hashlib.sha256(raw_path.read_bytes()).hexdigest() != page.get("raw_sha256"):
                    return None
            except OSError:
                return None
        return state

    def _relative(self, path: Path) -> str:
        return path.relative_to(self.data_dir).as_posix()
