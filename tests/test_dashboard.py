import json
import threading
import unittest
import urllib.error
import urllib.request
from dataclasses import replace

from netdoctor.config import load_config
from netdoctor.dashboard import DashboardState, create_server
from netdoctor.models import RunResult


def fake_measure(config):
    result = RunResult.demo(config.target.name, config.target.host, config.repetitions)
    return replace(result, mode="measured")


class DashboardApiTests(unittest.TestCase):
    def start(self, *, enabled=True, measure=fake_measure):
        state = DashboardState(load_config(), measurement_enabled=enabled, measure=measure)
        server = create_server("127.0.0.1", 0, state)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.base = f"http://127.0.0.1:{server.server_address[1]}"
        return state

    def request(self, path, body=None, content_type="application/json"):
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(self.base + path, data=data)
        if data is not None:
            request.add_header("Content-Type", content_type)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.read()

    def test_index_page_is_served(self):
        self.start()
        status, body = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn("/api/measure", body.decode("utf-8"))

    def test_config_exposes_target_but_not_download_url(self):
        self.start()
        status, body = self.request("/api/config")
        payload = json.loads(body)
        self.assertEqual(status, 200)
        self.assertTrue(payload["measurement_enabled"])
        self.assertEqual(payload["target"]["host"], "example.com")
        self.assertNotIn("download_url", payload["target"])

    def test_measure_returns_result_and_stores_last(self):
        self.start()
        self.assertEqual(self.request("/api/last")[0], 404)
        status, body = self.request("/api/measure", {"repetitions": 7})
        payload = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(payload["mode"], "measured")
        self.assertEqual(payload["repetitions_requested"], 7)
        status, body = self.request("/api/last")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["run_id"], payload["run_id"])

    def test_measure_is_refused_without_config(self):
        self.start(enabled=False)
        status, body = self.request("/api/measure", {})
        self.assertEqual(status, 403)
        self.assertIn("--config", json.loads(body)["error"])

    def test_measure_rejects_out_of_range_repetitions(self):
        self.start()
        for value in (4, 11, "7", True):
            with self.subTest(value=value):
                self.assertEqual(self.request("/api/measure", {"repetitions": value})[0], 400)

    def test_measure_ignores_target_fields_from_browser(self):
        seen = []

        def record(config):
            seen.append(config.target.host)
            return fake_measure(config)

        self.start(measure=record)
        status, _ = self.request("/api/measure", {"host": "evil.example", "repetitions": 5})
        self.assertEqual(status, 200)
        self.assertEqual(seen, ["example.com"])

    def test_measure_requires_json_content_type(self):
        self.start()
        status, _ = self.request("/api/measure", {}, content_type="text/plain")
        self.assertEqual(status, 415)

    def test_concurrent_measurement_is_rejected(self):
        state = self.start()
        state.lock.acquire()
        self.addCleanup(state.lock.release)
        self.assertEqual(self.request("/api/measure", {})[0], 409)

    def test_measure_crash_returns_500_and_releases_lock(self):
        def crash(_config):
            raise RuntimeError("boom")

        state = self.start(measure=crash)
        status, body = self.request("/api/measure", {})
        self.assertEqual(status, 500)
        self.assertIn("boom", json.loads(body)["error"])
        self.assertFalse(state.lock.locked())


if __name__ == "__main__":
    unittest.main()
