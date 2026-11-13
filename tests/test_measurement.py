import http.server
import threading
import unittest

from netdoctor.config import AppConfig, TargetConfig
from netdoctor.measurement import (
    Endpoint,
    MeasurementError,
    NetworkMeasurer,
    parse_ping_rtt,
    run_diagnostics,
)


class _FakeBackend:
    def __init__(self) -> None:
        self.ping_calls = 0
        self.tcp_calls = 0

    def resolve(self, host, port, timeout):
        return 10.0, (Endpoint(2, "127.0.0.1", port),)

    def tcp(self, endpoint, timeout):
        self.tcp_calls += 1
        return 20.0

    def http_ttfb(self, url, endpoint, timeout):
        return 30.0, 40.0, 200

    def download(self, url, timeout, max_bytes, max_seconds):
        return 50.0

    def ping(self, host, timeout):
        self.ping_calls += 1
        return 6.0


class _PartiallyFailingBackend(_FakeBackend):
    def tcp(self, endpoint, timeout):
        self.tcp_calls += 1
        if self.tcp_calls == 2:
            raise MeasurementError("simulated TCP timeout")
        return 20.0

    def ping(self, host, timeout):
        self.ping_calls += 1
        if self.ping_calls in {2, 4}:
            raise MeasurementError("simulated ICMP loss")
        return 6.0


class _BlockedPingBackend(_FakeBackend):
    def ping(self, host, timeout):
        raise MeasurementError("simulated ICMP blocking")


class _FallbackBackend(_FakeBackend):
    def resolve(self, host, port, timeout):
        return 10.0, (
            Endpoint(2, "192.0.2.1", port),
            Endpoint(2, "127.0.0.1", port),
        )

    def tcp(self, endpoint, timeout):
        if endpoint.ip == "192.0.2.1":
            raise MeasurementError("first address unavailable")
        return 20.0

    def http_ttfb(self, url, endpoint, timeout):
        if endpoint.ip == "192.0.2.1":
            raise MeasurementError("first address unavailable")
        return 30.0, 40.0, 200


def _config(*, download=True) -> AppConfig:
    return AppConfig(
        repetitions=5,
        timeout_seconds=1.0,
        pause_between_runs_seconds=0,
        target=TargetConfig(
            "local",
            "127.0.0.1",
            443,
            "https://127.0.0.1/",
            "http://127.0.0.1/data" if download else None,
        ),
        download_max_bytes=100_000,
        download_max_seconds=1.0,
    )


class OrchestratorTests(unittest.TestCase):
    def test_all_measurements_produce_samples(self) -> None:
        result = run_diagnostics(_config(), backend=_FakeBackend())
        metrics = {metric.key: metric for metric in result.metrics}
        self.assertEqual(result.mode, "measured")
        self.assertEqual(result.schema_version, "1.1")
        for key in ("dns_ms", "tcp_ms", "tls_ms", "ttfb_ms", "download_mbps", "rtt_ms"):
            self.assertEqual(metrics[key].status, "success")
            self.assertEqual(metrics[key].summary.count, 5)
        self.assertEqual(metrics["packet_loss_percent"].samples, (0.0,))

    def test_partial_failures_do_not_stop_later_runs(self) -> None:
        result = run_diagnostics(_config(), backend=_PartiallyFailingBackend())
        metrics = {metric.key: metric for metric in result.metrics}
        self.assertEqual(metrics["tcp_ms"].status, "partial")
        self.assertEqual(metrics["tcp_ms"].summary.count, 4)
        self.assertEqual(metrics["rtt_ms"].status, "partial")
        self.assertEqual(metrics["rtt_ms"].summary.count, 3)
        self.assertEqual(metrics["packet_loss_percent"].samples, (40.0,))

    def test_missing_download_url_is_unavailable_not_failed(self) -> None:
        result = run_diagnostics(_config(download=False), backend=_FakeBackend())
        metric = next(item for item in result.metrics if item.key == "download_mbps")
        self.assertEqual(metric.status, "unavailable")
        self.assertIn("not configured", metric.error)

    def test_all_ping_failures_do_not_claim_one_hundred_percent_loss(self) -> None:
        result = run_diagnostics(_config(), backend=_BlockedPingBackend())
        metrics = {metric.key: metric for metric in result.metrics}
        self.assertEqual(metrics["rtt_ms"].status, "failed")
        self.assertEqual(metrics["packet_loss_percent"].status, "unavailable")
        self.assertIn("ICMP blocking", metrics["packet_loss_percent"].error)

    def test_later_resolved_address_is_used_when_first_address_fails(self) -> None:
        result = run_diagnostics(_config(), backend=_FallbackBackend())
        metrics = {metric.key: metric for metric in result.metrics}
        self.assertEqual(metrics["tcp_ms"].status, "success")
        self.assertEqual(metrics["ttfb_ms"].status, "success")
        self.assertEqual(metrics["tcp_ms"].summary.count, 5)


class PingParserTests(unittest.TestCase):
    def test_parses_windows_and_unix_output(self) -> None:
        self.assertEqual(parse_ping_rtt("Reply: time=18ms TTL=57"), 18.0)
        self.assertEqual(parse_ping_rtt("64 bytes: time=12.4 ms"), 12.4)
        self.assertEqual(parse_ping_rtt("시간<1ms TTL=128"), 0.5)

    def test_rejects_output_without_rtt(self) -> None:
        with self.assertRaises(MeasurementError):
            parse_ping_rtt("Request timed out")


class _Handler(http.server.BaseHTTPRequestHandler):
    body = b"x" * 262_144

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, format, *args):
        pass


class _LocalBackend(NetworkMeasurer):
    def ping(self, host, timeout):
        return 0.1


class LocalNetworkMeasurementTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def test_tcp_ttfb_and_download_against_local_server(self) -> None:
        port = self.server.server_address[1]
        measurer = NetworkMeasurer()
        dns_ms, endpoints = measurer.resolve("127.0.0.1", port, 1.0)
        self.assertGreaterEqual(dns_ms, 0)
        self.assertGreater(measurer.tcp(endpoints[0], 1.0), 0)
        url = f"http://127.0.0.1:{port}/한글?query=값"
        ttfb_ms, tls_ms, status = measurer.http_ttfb(url, endpoints[0], 1.0)
        self.assertGreater(ttfb_ms, 0)
        self.assertIsNone(tls_ms)
        self.assertEqual(status, 200)
        self.assertGreater(measurer.download(url, 1.0, 100_000, 1.0), 0)

    def test_full_orchestrator_against_local_http_server(self) -> None:
        port = self.server.server_address[1]
        config = AppConfig(
            repetitions=5,
            timeout_seconds=1.0,
            pause_between_runs_seconds=0,
            target=TargetConfig(
                "local integration",
                "127.0.0.1",
                port,
                f"http://127.0.0.1:{port}/",
                f"http://127.0.0.1:{port}/download",
            ),
            download_max_bytes=100_000,
            download_max_seconds=1.0,
        )
        result = run_diagnostics(config, backend=_LocalBackend())
        metrics = {metric.key: metric for metric in result.metrics}
        for key in ("dns_ms", "tcp_ms", "ttfb_ms", "download_mbps", "rtt_ms"):
            self.assertEqual(metrics[key].status, "success")
            self.assertEqual(metrics[key].summary.count, 5)
        self.assertEqual(metrics["tls_ms"].status, "unavailable")


if __name__ == "__main__":
    unittest.main()
