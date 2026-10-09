from __future__ import annotations

import concurrent.futures
import dataclasses
import datetime as dt
import hashlib
import json
import logging
import os
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import deque
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


SCHEMA_VERSION = "1.0.0"
COLLECTOR_VERSION = "1.0.0"
DEFAULT_API_URL = "https://api.deps.dev"
LOGGER = logging.getLogger("collector.depsdev")


class CollectionError(RuntimeError):
    """A deps.dev response cannot be interpreted as an npm dependency graph."""


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
class GraphResult:
    records: Tuple[Dict[str, Any], ...]
    root_package: str
    root_version: str
    nodes: int
    edges: int
    direct_edges: int
    skipped_edges: int
    graph_error: bool
    nodes_with_errors: int
    bundled_nodes: int
    warnings: Tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class PackageVersionResult:
    requested_package: str
    requested_version: str
    snapshot_id: str
    root_package: str
    root_version: str
    raw_path: str
    normalized_path: str
    raw_bytes: int
    nodes: int
    edges: int
    direct_edges: int
    normalized_edges: int
    skipped_edges: int
    graph_error: bool
    nodes_with_errors: int
    bundled_nodes: int

    def to_result(self, status: str = "downloaded") -> Dict[str, Any]:
        return {"package_name": self.requested_package, "version": self.requested_version, "status": status, **dataclasses.asdict(self)}


@dataclasses.dataclass
class RunSummary:
    run_id: str
    target: int
    processed: int = 0
    api_successes: int = 0
    api_failures: int = 0
    downloaded: int = 0
    skipped: int = 0
    usable_graphs: int = 0
    graphs_with_errors: int = 0
    dependency_edges: int = 0
    direct_dependency_edges: int = 0
    normalized_edges: int = 0
    skipped_edges: int = 0
    nodes_with_errors: int = 0
    bundled_nodes: int = 0
    raw_bytes: int = 0
    elapsed_seconds: float = 0.0
    manifest_path: str = ""
    log_path: str = ""

    def to_dict(self) -> Dict[str, Any]:
        result = dataclasses.asdict(self)
        result["success_rate"] = round(self.api_successes / self.processed, 6) if self.processed else 0.0
        result["versions_per_second"] = round(self.processed / self.elapsed_seconds, 3) if self.elapsed_seconds else 0.0
        return result


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
                handle.flush()


class HttpClient:
    def __init__(self, timeout: float = 30.0, retries: int = 3):
        self.timeout = timeout
        self.retries = retries
        self.user_agent = f"bigdata-depsdev-collector/{COLLECTOR_VERSION}"

    def get(self, url: str) -> HttpResponse:
        last_error: Optional[BaseException] = None
        for attempt in range(self.retries + 1):
            request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": self.user_agent}, method="GET")
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    return HttpResponse(
                        url=response.geturl(),
                        status=response.status,
                        body=response.read(),
                        headers={key.lower(): value for key, value in response.headers.items()},
                    )
            except urllib.error.HTTPError as exc:
                last_error = exc
                retryable = exc.code == 429 or 500 <= exc.code <= 599
                if not retryable or attempt == self.retries:
                    raise FetchError(url, f"HTTP {exc.code} for {url}", status=exc.code) from exc
                delay = retry_delay(attempt, exc.headers.get("Retry-After"))
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = exc
                if attempt == self.retries:
                    raise FetchError(url, f"request failed for {url}: {exc}") from exc
                delay = retry_delay(attempt, None)
            time.sleep(delay)
        raise FetchError(url, f"request failed for {url}: {last_error}")


def retry_delay(attempt: int, retry_after: Optional[str]) -> float:
    if retry_after:
        try:
            return min(60.0, max(0.0, float(retry_after)))
        except ValueError:
            pass
    return min(30.0, (2**attempt) + random.random())


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parser_version() -> str:
    digest = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    return f"depsdev-collector@{COLLECTOR_VERSION}+source.{digest}"


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


def _jsonl_bytes(records: Iterable[Mapping[str, Any]]) -> bytes:
    return b"".join(_json_bytes(record) for record in records)


def _safe_json_object(body: bytes, context: str) -> Dict[str, Any]:
    try:
        decoded = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CollectionError(f"invalid JSON in {context}: {exc}") from exc
    if not isinstance(decoded, dict):
        raise CollectionError(f"expected a JSON object in {context}")
    return decoded


def _index(value: Any) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _version_key(nodes: Sequence[Any], index: int) -> Optional[Tuple[str, str, str]]:
    if index >= len(nodes) or not isinstance(nodes[index], dict):
        return None
    key = nodes[index].get("versionKey")
    if not isinstance(key, dict):
        return None
    system, name, version = key.get("system"), key.get("name"), key.get("version")
    if not all(isinstance(item, str) and item for item in (system, name, version)):
        return None
    return system, name, version


def _node_has_errors(node: Any) -> bool:
    return isinstance(node, dict) and isinstance(node.get("errors"), list) and bool(node["errors"])


def _node_is_bundled(node: Any) -> bool:
    return isinstance(node, dict) and node.get("bundled") is True


def normalize_graph(document: Mapping[str, Any], provenance: Mapping[str, Any]) -> GraphResult:
    nodes = document.get("nodes")
    edges = document.get("edges")
    if not isinstance(nodes, list) or not nodes:
        raise CollectionError("deps.dev graph has no non-empty nodes array")
    if not isinstance(edges, list):
        raise CollectionError("deps.dev graph has no edges array")

    root_key = _version_key(nodes, 0)
    if root_key is None or root_key[0] != "NPM":
        raise CollectionError("deps.dev graph root is not a valid NPM version key")
    _, root_package, root_version = root_key
    graph_error = isinstance(document.get("error"), str) and bool(document["error"])
    nodes_with_errors = sum(1 for node in nodes if _node_has_errors(node))
    bundled_nodes = sum(1 for node in nodes if _node_is_bundled(node))
    warnings: List[str] = []

    valid_edges: List[Tuple[int, int, int, Mapping[str, Any]]] = []
    adjacency: List[List[int]] = [[] for _ in nodes]
    for edge_index, edge in enumerate(edges):
        if not isinstance(edge, dict):
            warnings.append(f"ignored malformed edge[{edge_index}]")
            continue
        from_node, to_node = _index(edge.get("fromNode")), _index(edge.get("toNode"))
        if from_node is None or to_node is None or from_node >= len(nodes) or to_node >= len(nodes):
            warnings.append(f"ignored edge[{edge_index}] with invalid node index")
            continue
        valid_edges.append((edge_index, from_node, to_node, edge))
        adjacency[from_node].append(to_node)

    distances: List[Optional[int]] = [None] * len(nodes)
    distances[0] = 0
    queue = deque([0])
    while queue:
        current = queue.popleft()
        assert distances[current] is not None
        for target in adjacency[current]:
            if distances[target] is None:
                distances[target] = distances[current] + 1
                queue.append(target)

    common = {"schema_version": SCHEMA_VERSION, "source": "depsdev", "provenance": dict(provenance)}
    records: List[Dict[str, Any]] = []
    skipped_edges = len(edges) - len(valid_edges)
    if graph_error:
        skipped_edges = len(edges)
        warnings.append(f"graph error: {document['error']}")
    else:
        for edge_index, from_node, to_node, _edge in valid_edges:
            source_key, dependency_key = _version_key(nodes, from_node), _version_key(nodes, to_node)
            if source_key is None or dependency_key is None or source_key[0] != "NPM" or dependency_key[0] != "NPM":
                skipped_edges += 1
                warnings.append(f"ignored edge[{edge_index}] with invalid NPM version key")
                continue
            if _node_is_bundled(nodes[from_node]) or _node_is_bundled(nodes[to_node]):
                skipped_edges += 1
                continue
            if _node_has_errors(nodes[from_node]) or _node_has_errors(nodes[to_node]):
                skipped_edges += 1
                continue
            depth = distances[to_node]
            if depth is None or depth < 1:
                skipped_edges += 1
                warnings.append(f"ignored edge[{edge_index}] not reachable from root")
                continue
            records.append(
                {
                    **common,
                    "root_package": root_package,
                    "root_version": root_version,
                    "edge_index": edge_index,
                    "from_node": from_node,
                    "to_node": to_node,
                    "source_package": source_key[1],
                    "source_version": source_key[2],
                    "dependency_package": dependency_key[1],
                    "resolved_version": dependency_key[2],
                    "depth": depth,
                }
            )

    return GraphResult(
        records=tuple(records),
        root_package=root_package,
        root_version=root_version,
        nodes=len(nodes),
        edges=len(edges),
        direct_edges=sum(1 for _, from_node, _, _ in valid_edges if from_node == 0),
        skipped_edges=skipped_edges,
        graph_error=graph_error,
        nodes_with_errors=nodes_with_errors,
        bundled_nodes=bundled_nodes,
        warnings=tuple(warnings),
    )


def _state_key(package_name: str, version: str) -> str:
    return hashlib.sha256(f"{package_name}\0{version}".encode("utf-8")).hexdigest()


class DepsDevCollector:
    def __init__(
        self,
        data_dir: Path,
        api_url: str = DEFAULT_API_URL,
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
        self.client = client or HttpClient(timeout=timeout, retries=retries)
        self.parser_version = parser_version()
        self._event_log: Optional[JsonEventLog] = None

    def run(self, package_versions: Sequence[Tuple[str, str]]) -> RunSummary:
        requested = list(dict.fromkeys(package_versions))
        if not requested:
            raise ValueError("at least one package/version is required")
        if any(not package_name or not version for package_name, version in requested):
            raise ValueError("package names and versions must be non-empty")

        run_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid.uuid4().hex[:8]
        log_path = self.data_dir / "logs" / "depsdev" / f"{run_id}.jsonl"
        manifest_path = self.data_dir / "manifests" / "depsdev" / f"{run_id}.json"
        self._event_log = JsonEventLog(log_path)
        summary = RunSummary(run_id=run_id, target=len(requested), manifest_path=self._relative(manifest_path), log_path=self._relative(log_path))
        started_at, started = utc_now(), time.monotonic()
        results_by_index: Dict[int, Dict[str, Any]] = {}
        pending: List[Tuple[int, str, str]] = []
        self._event_log.write("run_started", run_id=run_id, target=summary.target, api_url=self.api_url, workers=self.workers, parser_version=self.parser_version)

        for index, (package_name, version) in enumerate(requested):
            state = None if self.refresh else self._complete_state(package_name, version)
            if state is None:
                pending.append((index, package_name, version))
                continue
            result = {"package_name": package_name, "version": version, "status": "skipped", **state}
            results_by_index[index] = result
            self._apply_success(summary, result, skipped=True)
            self._event_log.write("version_skipped", run_id=run_id, **result)

        with concurrent.futures.ThreadPoolExecutor(max_workers=self.workers) as executor:
            futures = {executor.submit(self._collect_one, package_name, version, run_id): (index, package_name, version) for index, package_name, version in pending}
            for future in concurrent.futures.as_completed(futures):
                index, package_name, version = futures[future]
                summary.processed += 1
                try:
                    collected = future.result()
                except Exception as exc:  # request isolation is intentional
                    summary.api_failures += 1
                    result = {"package_name": package_name, "version": version, "status": "failed", "error_type": type(exc).__name__, "error": str(exc), "http_status": getattr(exc, "status", None)}
                    LOGGER.error("deps.dev query failed for %s@%s: %s", package_name, version, exc)
                    self._event_log.write("version_failed", run_id=run_id, **result)
                else:
                    result = collected.to_result()
                    self._apply_success(summary, result, skipped=False, increment_processed=False)
                    self._event_log.write("version_downloaded", run_id=run_id, **result)
                results_by_index[index] = result
                if summary.processed % 25 == 0 or summary.processed == summary.target:
                    LOGGER.info("deps.dev progress: processed=%d/%d success=%d failed=%d edges=%d", summary.processed, summary.target, summary.api_successes, summary.api_failures, summary.dependency_edges)

        summary.elapsed_seconds = round(time.monotonic() - started, 3)
        manifest = {
            "run_id": run_id,
            "started_at": started_at,
            "finished_at": utc_now(),
            "source": "depsdev",
            "api_url": self.api_url,
            "endpoint": "/v3/systems/npm/packages/{name}/versions/{version}:dependencies",
            "parser_version": self.parser_version,
            "summary": summary.to_dict(),
            "results": [results_by_index[index] for index in range(len(requested))],
        }
        _atomic_write(manifest_path, _json_bytes(manifest, pretty=True))
        self._event_log.write("run_finished", **summary.to_dict())
        return summary

    def _apply_success(self, summary: RunSummary, result: Mapping[str, Any], skipped: bool, increment_processed: bool = True) -> None:
        if increment_processed:
            summary.processed += 1
        summary.api_successes += 1
        if skipped:
            summary.skipped += 1
        else:
            summary.downloaded += 1
        if result.get("graph_error"):
            summary.graphs_with_errors += 1
        else:
            summary.usable_graphs += 1
        summary.dependency_edges += int(result.get("edges", 0))
        summary.direct_dependency_edges += int(result.get("direct_edges", 0))
        summary.normalized_edges += int(result.get("normalized_edges", 0))
        summary.skipped_edges += int(result.get("skipped_edges", 0))
        summary.nodes_with_errors += int(result.get("nodes_with_errors", 0))
        summary.bundled_nodes += int(result.get("bundled_nodes", 0))
        summary.raw_bytes += int(result.get("raw_bytes", 0))

    def _collect_one(self, package_name: str, version: str, run_id: str) -> PackageVersionResult:
        encoded_name = urllib.parse.quote(package_name, safe="")
        encoded_version = urllib.parse.quote(version, safe="")
        request_url = f"{self.api_url}/v3/systems/npm/packages/{encoded_name}/versions/{encoded_version}:dependencies"
        response = self.client.get(request_url)
        retrieved_at, snapshot_id = utc_now(), str(uuid.uuid4())
        raw_sha256 = hashlib.sha256(response.body).hexdigest()
        raw_path = self.data_dir / "raw" / "depsdev" / retrieved_at[:10] / f"{snapshot_id}.json"
        _atomic_write(raw_path, response.body)
        provenance = {
            "snapshot_id": snapshot_id,
            "request_url": request_url,
            "retrieved_at": retrieved_at,
            "raw_path": self._relative(raw_path),
            "raw_sha256": raw_sha256,
            "parser_version": self.parser_version,
        }
        graph = normalize_graph(_safe_json_object(response.body, request_url), provenance)
        normalized_path = self.data_dir / "normalized" / "v1" / "dependency_relation" / retrieved_at[:10] / f"{snapshot_id}.jsonl"
        _atomic_write(normalized_path, _jsonl_bytes(graph.records))
        result = PackageVersionResult(
            requested_package=package_name,
            requested_version=version,
            snapshot_id=snapshot_id,
            root_package=graph.root_package,
            root_version=graph.root_version,
            raw_path=self._relative(raw_path),
            normalized_path=self._relative(normalized_path),
            raw_bytes=len(response.body),
            nodes=graph.nodes,
            edges=graph.edges,
            direct_edges=graph.direct_edges,
            normalized_edges=len(graph.records),
            skipped_edges=graph.skipped_edges,
            graph_error=graph.graph_error,
            nodes_with_errors=graph.nodes_with_errors,
            bundled_nodes=graph.bundled_nodes,
        )
        state = {**result.to_result(status="complete"), "request_url": request_url, "http_status": response.status, "raw_sha256": raw_sha256, "etag": response.headers.get("etag"), "parser_version": self.parser_version}
        _atomic_write(self._state_path(package_name, version), _json_bytes(state, pretty=True))
        assert self._event_log is not None
        self._event_log.write("graph_normalized", run_id=run_id, package_name=package_name, version=version, snapshot_id=snapshot_id, warnings=list(graph.warnings))
        return result

    def _state_path(self, package_name: str, version: str) -> Path:
        return self.data_dir / "state" / "depsdev" / f"{_state_key(package_name, version)}.json"

    def _complete_state(self, package_name: str, version: str) -> Optional[Dict[str, Any]]:
        try:
            state = json.loads(self._state_path(package_name, version).read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(state, dict) or state.get("requested_package") != package_name or state.get("requested_version") != version or state.get("parser_version") != self.parser_version:
            return None
        paths = (state.get("raw_path"), state.get("normalized_path"))
        if any(not isinstance(path, str) or not (self.data_dir / path).is_file() for path in paths):
            return None
        raw_path = self.data_dir / paths[0]
        try:
            if hashlib.sha256(raw_path.read_bytes()).hexdigest() != state.get("raw_sha256"):
                return None
        except OSError:
            return None
        return {key: value for key, value in state.items() if key not in {"package_name", "version", "status"}}

    def _relative(self, path: Path) -> str:
        return path.relative_to(self.data_dir).as_posix()
