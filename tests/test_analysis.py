import unittest
from dataclasses import replace

from netdoctor.analysis import analyze
from netdoctor.models import MetricResult, RunResult


def _scenario(**values: float | None) -> RunResult:
    definitions = (
        ("dns_ms", "DNS", "ms"),
        ("tcp_ms", "TCP 연결", "ms"),
        ("ttfb_ms", "TTFB", "ms"),
        ("download_mbps", "다운로드", "Mbps"),
        ("rtt_ms", "RTT", "ms"),
        ("packet_loss_percent", "패킷 손실", "%"),
    )
    metrics = []
    for key, label, unit in definitions:
        value = values.get(key)
        if value is None:
            metrics.append(MetricResult(key, label, unit, (), "unavailable", "test scenario"))
        else:
            metrics.append(MetricResult(key, label, unit, (value,) * 5))
    return replace(RunResult.demo("scenario", "example.test", 5), metrics=tuple(metrics))


class AnalysisRuleTests(unittest.TestCase):
    def test_healthy_scenario_has_good_rating_and_no_bottleneck(self) -> None:
        analysis = analyze(
            _scenario(
                dns_ms=12,
                tcp_ms=22,
                ttfb_ms=90,
                download_mbps=80,
                rtt_ms=20,
                packet_loss_percent=0,
            )
        )
        self.assertEqual(analysis.overall_rating, "good")
        self.assertEqual(analysis.primary_bottleneck, "없음")
        self.assertEqual(analysis.findings[0].code, "no_major_bottleneck")
        self.assertTrue(analysis.findings[0].evidence)
        self.assertTrue(analysis.findings[0].uncertainty)

    def test_slow_dns_is_reported_with_observed_evidence(self) -> None:
        analysis = analyze(
            _scenario(
                dns_ms=140,
                tcp_ms=20,
                ttfb_ms=100,
                download_mbps=70,
                rtt_ms=20,
                packet_loss_percent=0,
            )
        )
        finding = next(item for item in analysis.findings if item.code == "dns_delay")
        self.assertEqual(analysis.overall_rating, "fair")
        self.assertEqual(finding.evidence[0].value, 140)
        self.assertIn("캐시", finding.uncertainty)

    def test_high_rtt_and_low_throughput_combination_is_critical(self) -> None:
        analysis = analyze(
            _scenario(
                dns_ms=20,
                tcp_ms=180,
                ttfb_ms=250,
                download_mbps=2,
                rtt_ms=240,
                packet_loss_percent=0,
            )
        )
        finding = next(
            item for item in analysis.findings if item.code == "latency_and_throughput"
        )
        self.assertEqual(finding.severity, "critical")
        self.assertEqual(analysis.overall_rating, "poor")
        self.assertEqual(len(finding.evidence), 2)

    def test_low_rtt_and_low_throughput_suggests_access_or_server_limit(self) -> None:
        analysis = analyze(
            _scenario(
                dns_ms=10,
                tcp_ms=15,
                ttfb_ms=80,
                download_mbps=6,
                rtt_ms=18,
                packet_loss_percent=0,
            )
        )
        codes = {item.code for item in analysis.findings}
        self.assertIn("low_throughput_near_path", codes)
        self.assertEqual(analysis.primary_bottleneck, "액세스 링크 또는 서버 제한 가능성")

    def test_packet_loss_reduces_conference_rating(self) -> None:
        healthy = analyze(
            _scenario(
                dns_ms=10,
                tcp_ms=15,
                ttfb_ms=80,
                download_mbps=50,
                rtt_ms=30,
                packet_loss_percent=0,
            )
        )
        lossy = analyze(
            _scenario(
                dns_ms=10,
                tcp_ms=15,
                ttfb_ms=80,
                download_mbps=50,
                rtt_ms=30,
                packet_loss_percent=6,
            )
        )
        healthy_stars = next(
            item.stars for item in healthy.suitability if item.key == "video_conference"
        )
        lossy_stars = next(
            item.stars for item in lossy.suitability if item.key == "video_conference"
        )
        self.assertLess(lossy_stars, healthy_stars)

    def test_missing_metrics_produce_unknown_without_false_certainty(self) -> None:
        analysis = analyze(
            _scenario(
                dns_ms=10,
                tcp_ms=None,
                ttfb_ms=None,
                download_mbps=None,
                rtt_ms=None,
                packet_loss_percent=None,
            )
        )
        self.assertEqual(analysis.overall_rating, "unknown")
        self.assertEqual(analysis.suitability[1].stars, 0)
        self.assertTrue(analysis.findings[0].evidence)
        self.assertTrue(any(item.value is None for item in analysis.findings[0].evidence))
        self.assertTrue(all(rating.uncertainty for rating in analysis.suitability))


if __name__ == "__main__":
    unittest.main()
