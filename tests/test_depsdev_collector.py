from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from collector.__main__ import _package_versions, main
from collector.depsdev.collector import DepsDevCollector, HttpClient, HttpResponse, RunSummary, normalize_graph, retry_delay


GRAPH = {
    "nodes": [
        {"versionKey": {"system": "NPM", "name": "@scope/root", "version": "1.0.0+meta"}, "bundled": False, "relation": "SELF", "errors": []},
        {"versionKey": {"system": "NPM", "name": "direct-a", "version": "1.2.0"}, "bundled": False, "relation": "DIRECT", "errors": []},
        {"versionKey": {"system": "NPM", "name": "direct-b", "version": "2.1.0"}, "bundled": False, "relation": "DIRECT", "errors": []},
        {"versionKey": {"system": "NPM", "name": "indirect-c", "version": "3.0.0"}, "bundled": False, "relation": "INDIRECT", "errors": []},
        {"versionKey": {"system": "NPM", "name": "root>bundled", "version": "4.0.0"}, "bundled": True, "relation": "INDIRECT", "errors": []},
    ],
    "edges": [
        {"fromNode": 0, "toNode": 1, "requirement": "^1.0.0"},
        {"fromNode": 0, "toNode": 2, "requirement": "^2.0.0"},
        {"fromNode": 1, "toNode": 2, "requirement": "~2.1.0"},
        {"fromNode": 2, "toNode": 3, "requirement": "3.x"},
        {"fromNode": 3, "toNode": 4, "requirement": "4.0.0"},
    ],
    "error": "",
}


class FakeClient:
    def __init__(self, values):
        self.values = list(values)
        self.calls = []

    def get(self, url):
        self.calls.append(url)
        value = self.values.pop(0)
        if isinstance(value, Exception):
            raise value
        body = json.dumps(value, separators=(",", ":")).encode("utf-8")
        return HttpResponse(url=url, status=200, body=body, headers={"etag": "test"})


class NoNetworkClient:
    def get(self, url):
        raise AssertionError(f"unexpected request: {url}")


class UrlopenResponse:
    status = 200
    headers = {}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def geturl(self):
        return "https://deps.example/final"

    def read(self):
        return b"{}"


class DepsDevCollectorTest(unittest.TestCase):
    def test_raw_graph_normalization_shortest_depth_and_resume(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data"
            client = FakeClient([GRAPH])
            summary = DepsDevCollector(data_dir, api_url="https://deps.example", workers=1, client=client).run([("@scope/root", "1.0.0+meta")])

            self.assertEqual((summary.processed, summary.api_successes, summary.api_failures), (1, 1, 0))
            self.assertEqual((summary.dependency_edges, summary.direct_dependency_edges, summary.normalized_edges, summary.skipped_edges), (5, 2, 4, 1))
            self.assertIn("%40scope%2Froot", client.calls[0])
            self.assertIn("1.0.0%2Bmeta:dependencies", client.calls[0])

            raw_files = list((data_dir / "raw" / "depsdev").rglob("*.json"))
            self.assertEqual(len(raw_files), 1)
            self.assertEqual(json.loads(raw_files[0].read_text(encoding="utf-8")), GRAPH)
            records = [json.loads(line) for path in (data_dir / "normalized" / "v1" / "dependency_relation").rglob("*.jsonl") for line in path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(records), 4)
            self.assertEqual(records[2]["edge_index"], 2)
            self.assertEqual(records[2]["depth"], 1)
            self.assertEqual(records[3]["depth"], 2)
            self.assertTrue(all(record["source"] == "depsdev" for record in records))

            resumed = DepsDevCollector(data_dir, api_url="https://deps.example", workers=1, client=NoNetworkClient()).run([("@scope/root", "1.0.0+meta")])
            self.assertEqual((resumed.processed, resumed.downloaded, resumed.skipped, resumed.api_successes), (1, 0, 1, 1))

            raw_files[0].write_bytes(b"{}\n")
            repaired = DepsDevCollector(data_dir, api_url="https://deps.example", workers=1, client=FakeClient([GRAPH])).run([("@scope/root", "1.0.0+meta")])
            self.assertEqual((repaired.processed, repaired.downloaded, repaired.skipped, repaired.api_successes), (1, 1, 0, 1))
            state_path = next((data_dir / "state" / "depsdev").glob("*.json"))
            active_raw_path = data_dir / json.loads(state_path.read_text(encoding="utf-8"))["raw_path"]
            self.assertEqual(json.loads(active_raw_path.read_text(encoding="utf-8")), GRAPH)
            self.assertEqual(len(list((data_dir / "raw" / "depsdev").rglob("*.json"))), 2)

    def test_api_error_isolated_and_manifest_keeps_input_order(self):
        empty_graph = {"nodes": [{"versionKey": {"system": "NPM", "name": "good", "version": "1.0.0"}, "bundled": False, "relation": "SELF", "errors": []}], "edges": [], "error": ""}
        with tempfile.TemporaryDirectory() as temporary:
            client = FakeClient([RuntimeError("offline"), empty_graph])
            data_dir = Path(temporary) / "data"
            summary = DepsDevCollector(data_dir, workers=1, client=client).run([("broken", "1.0.0"), ("good", "1.0.0")])
            self.assertEqual((summary.processed, summary.api_successes, summary.api_failures), (2, 1, 1))
            manifest_path = next((data_dir / "manifests" / "depsdev").glob("*.json"))
            results = json.loads(manifest_path.read_text(encoding="utf-8"))["results"]
            self.assertEqual([result["package_name"] for result in results], ["broken", "good"])
            self.assertEqual([result["status"] for result in results], ["failed", "downloaded"])

    def test_graph_error_and_node_errors_are_not_normalized(self):
        provenance = {"snapshot_id": "test"}
        graph_error = dict(GRAPH, error="resolution failed")
        result = normalize_graph(graph_error, provenance)
        self.assertTrue(result.graph_error)
        self.assertEqual((len(result.records), result.skipped_edges), (0, 5))

        node_error = json.loads(json.dumps(GRAPH))
        node_error["nodes"][2]["errors"] = ["unresolved requirement"]
        result = normalize_graph(node_error, provenance)
        self.assertEqual(result.nodes_with_errors, 1)
        self.assertEqual([record["edge_index"] for record in result.records], [0])
        self.assertEqual(result.skipped_edges, 4)

    def test_package_version_parser_and_retry_after(self):
        self.assertEqual(_package_versions(["@scope/pkg@1.2.3", "plain@2.0.0", "plain@2.0.0"], None), [("@scope/pkg", "1.2.3"), ("plain", "2.0.0")])
        self.assertEqual(retry_delay(0, "4.5"), 4.5)
        self.assertEqual(retry_delay(10, "100"), 60.0)

    def test_cli_success_limit_allows_isolated_api_failures(self):
        summary = RunSummary(run_id="test", target=2, processed=2, api_successes=1, api_failures=1)
        arguments = [
            "collect",
            "depsdev",
            "--package-version",
            "good@1.0.0",
            "--package-version",
            "missing@1.0.0",
        ]

        with mock.patch("collector.__main__.DepsDevCollector") as collector:
            collector.return_value.run.return_value = summary
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(arguments), 1)

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main([*arguments, "--success-limit", "1"]), 0)

        result = json.loads(output.getvalue())
        self.assertEqual(result["success_limit"], 1)
        self.assertTrue(result["success_target_met"])

    def test_cli_success_limit_returns_nonzero_when_target_is_not_met(self):
        summary = RunSummary(run_id="test", target=2, processed=2, api_successes=1, api_failures=1)
        with mock.patch("collector.__main__.DepsDevCollector") as collector:
            collector.return_value.run.return_value = summary
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                exit_code = main(["collect", "depsdev", "--package-version", "good@1.0.0", "--success-limit", "2"])

        self.assertEqual(exit_code, 1)
        self.assertFalse(json.loads(output.getvalue())["success_target_met"])

    def test_http_client_retries_temporary_network_error(self):
        with mock.patch("collector.depsdev.collector.urllib.request.urlopen", side_effect=[urllib.error.URLError("temporary"), UrlopenResponse()]) as urlopen:
            with mock.patch("collector.depsdev.collector.time.sleep") as sleep:
                response = HttpClient(timeout=1.0, retries=1).get("https://deps.example/graph")
        self.assertEqual(response.status, 200)
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once()


if __name__ == "__main__":
    unittest.main()
