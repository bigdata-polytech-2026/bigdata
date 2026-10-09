from __future__ import annotations

import contextlib
import io
import json
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from collector.__main__ import main


PACKUMENT = {
    "name": "example",
    "dist-tags": {"latest": "3.0.0"},
    "time": {
        "created": "2020-01-01T00:00:00Z",
        "modified": "2022-01-01T00:00:00Z",
        "1.0.0": "2020-01-01T00:00:00Z",
        "2.0.0": "2021-01-01T00:00:00Z",
        "3.0.0": "2022-01-01T00:00:00Z",
    },
    "versions": {
        "1.0.0": {"dependencies": {"alpha": "^1.0.0"}},
        "2.0.0": {"dependencies": {"alpha": "^2.0.0"}},
        "3.0.0": {"dependencies": {"alpha": "^3.0.0"}},
    },
}


class PilotApiHandler(BaseHTTPRequestHandler):
    requests = 0

    def do_GET(self):
        type(self).requests += 1
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/-/v1/search":
            self._send({"objects": [{"package": {"name": "example"}}], "total": 1})
            return
        if parsed.path == "/example":
            self._send(PACKUMENT)
            return
        if parsed.path.endswith(":dependencies"):
            segments = parsed.path.split("/")
            package_name = urllib.parse.unquote(segments[-3])
            version = urllib.parse.unquote(segments[-1].removesuffix(":dependencies"))
            self._send({"nodes": [{"versionKey": {"system": "NPM", "name": package_name, "version": version}, "bundled": False, "relation": "SELF", "errors": []}], "edges": [], "error": ""})
            return
        self.send_error(404)

    def do_POST(self):
        type(self).requests += 1
        length = int(self.headers.get("Content-Length", "0"))
        query = json.loads(self.rfile.read(length))
        if self.path != "/v1/query":
            self.send_error(404)
            return
        if query["version"] == "1.0.0":
            self._send({"vulns": [{
                "id": "GHSA-pilot-test", "published": "2020-01-01T00:00:00Z", "modified": "2020-01-02T00:00:00Z",
                "affected": [{"package": {"ecosystem": "npm", "name": "example"}, "versions": ["1.0.0"]}],
            }]})
        else:
            self._send({})

    def _send(self, value):
        payload = json.dumps(value).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format, *args):
        return


class PilotIntegrationTest(unittest.TestCase):
    def test_one_command_collects_and_freezes_resumable_snapshot(self):
        PilotApiHandler.requests = 0
        server = ThreadingHTTPServer(("127.0.0.1", 0), PilotApiHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_port}"
        try:
            with tempfile.TemporaryDirectory() as temporary:
                data_root = Path(temporary) / "data"
                arguments = [
                    "collect", "pilot", "--dataset-id", "pilot-test", "--package-limit", "1",
                    "--versions-per-package", "2", "--chunk-size", "1", "--workers", "2", "--osv-workers", "2",
                    "--shard-size-mb", "1", "--data-root", str(data_root), "--registry-url", base_url,
                    "--depsdev-api-url", base_url, "--osv-api-url", base_url,
                ]
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.assertEqual(main(arguments), 0)
                summary = json.loads(output.getvalue())
                self.assertTrue(summary["criteria_met"])
                self.assertEqual((summary["packages"], summary["package_versions"], summary["selected_versions"]), (1, 3, 2))
                self.assertEqual((summary["depsdev_successes"], summary["osv_checked"]), (2, 2))
                self.assertTrue((data_root / "datasets" / "pilot-test" / "manifest.json").is_file())

                request_count = PilotApiHandler.requests
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(main(arguments), 0)
                self.assertEqual(PilotApiHandler.requests, request_count)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
