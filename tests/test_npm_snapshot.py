from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import pyarrow.parquet as pq

from collector.npm.collector import FetchError, HttpResponse
from collector.npm_snapshot.collector import NpmSnapshotCollector
from processing.normalization.pipeline import run as normalize


def packument(name: str, dependency: str) -> bytes:
    return json.dumps({
        "name": name,
        "dist-tags": {"latest": "1.0.0"},
        "time": {
            "created": "2020-01-01T00:00:00Z",
            "modified": "2020-01-01T00:00:00Z",
            "1.0.0": "2020-01-01T00:00:00Z",
        },
        "versions": {
            "1.0.0": {
                "name": name,
                "version": "1.0.0",
                "dependencies": {dependency: "^1.0.0"},
            }
        },
    }, separators=(",", ":")).encode()


class FakeClient:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def get(self, url):
        self.calls.append(url)
        return self.responses[url]


class FlakyClient:
    def __init__(self, response):
        self.response = response
        self.calls = 0

    def get(self, url):
        self.calls += 1
        if self.calls == 1:
            raise FetchError(url, "temporary network failure")
        return self.response


class NpmSnapshotCollectorTest(unittest.TestCase):
    def test_dependency_expansion_requeues_transient_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "data"
            base = "https://registry.example"
            client = FlakyClient(HttpResponse(f"{base}/app", 200, packument("app", "app"), {}))
            summary = NpmSnapshotCollector(
                root, "retry-test", package_limit=1, batch_size=1, workers=1,
                registry_url=base, expand_dependencies=True, client=client,
            ).run(package_names=["app"])
            self.assertEqual((summary.status, summary.packages, summary.failed), ("complete", 1, 1))
            self.assertEqual(client.calls, 2)
            failures = json.loads((root / "datasets/retry-test/inputs/failures.json").read_text())
            self.assertEqual(failures[0]["package"], "app")

    def test_dependency_expansion_reaches_success_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "data"
            base = "https://registry.example"
            client = FakeClient({
                f"{base}/app": HttpResponse(f"{base}/app", 200, packument("app", "lib"), {}),
                f"{base}/lib": HttpResponse(f"{base}/lib", 200, packument("lib", "app"), {}),
            })
            summary = NpmSnapshotCollector(
                root, "closure-test", package_limit=2, batch_size=1, workers=1,
                registry_url=base, expand_dependencies=True, client=client,
            ).run(package_names=["app"])
            self.assertEqual((summary.status, summary.packages, summary.package_target),
                             ("complete", 2, 2))
            self.assertEqual(client.calls, [f"{base}/app", f"{base}/lib"])
            frozen = (root / "datasets/closure-test/inputs/packages.txt").read_text().splitlines()
            self.assertEqual(frozen, ["app", "lib"])

    def test_streaming_batches_normalize_and_resume(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "data"
            base = "https://registry.example"
            client = FakeClient({
                f"{base}/app": HttpResponse(f"{base}/app", 200, packument("app", "lib"), {"etag": "app"}),
                f"{base}/lib": HttpResponse(f"{base}/lib", 200, packument("lib", "app"), {"etag": "lib"}),
            })
            collector = NpmSnapshotCollector(root, "full-test", package_limit=2,
                                             batch_size=1, workers=2,
                                             registry_url=base, client=client)
            summary = collector.run(package_names=["app", "lib"])
            self.assertEqual((summary.status, summary.packages, summary.batches), ("complete", 2, 2))
            self.assertEqual(len(client.calls), 2)
            manifest = json.loads((root / "datasets/full-test/manifest.json").read_text())
            self.assertEqual(len(manifest["shards"]["raw"]["npm"]), 2)
            self.assertEqual(len(manifest["shards"]["audit"]["npm"]), 2)
            self.assertEqual(manifest["shards"]["raw"]["depsdev"], [])
            self.assertFalse((root / "landing/.full-test-npm-snapshot/snapshot").exists())

            normalized = root / "datasets/full-test-normalized/normalized"
            report = normalize(root / "datasets/full-test", normalized)
            self.assertEqual(report["counts"], {
                "packages": 2,
                "versions": 2,
                "dependency_requirements": 2,
                "dependency_relations": 0,
            })
            self.assertTrue(all(value == 0 for value in report["integrity_violations"].values()))
            self.assertEqual(pq.read_table(normalized / "packages").num_rows, 2)

            no_network = FakeClient({})
            resumed = NpmSnapshotCollector(root, "full-test", package_limit=2,
                                            batch_size=1, workers=1,
                                            registry_url=base, client=no_network).run(
                                                package_names=["app", "lib"])
            self.assertEqual(resumed.to_dict(), summary.to_dict())
            self.assertEqual(no_network.calls, [])


if __name__ == "__main__":
    unittest.main()
