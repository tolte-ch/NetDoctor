"""Explain measurements with deterministic, evidence-carrying rules."""

from __future__ import annotations

from .models import (
    DiagnosticAnalysis,
    Evidence,
    Finding,
    MetricResult,
    RunResult,
    SuitabilityRating,
)


SINGLE_RUN_UNCERTAINTY = (
    "한 대상과 한 시점의 관측이므로 다른 서버·시간대·접속 방식에서는 결과가 달라질 수 있습니다."
)


def _metric_map(result: RunResult) -> dict[str, MetricResult]:
    return {metric.key: metric for metric in result.metrics}


def _value(metrics: dict[str, MetricResult], key: str) -> float | None:
    metric = metrics.get(key)
    if metric is None or metric.summary is None:
        return None
    return metric.summary.median


def _evidence(metrics: dict[str, MetricResult], key: str, observation: str) -> Evidence | None:
    metric = metrics.get(key)
    if metric is None:
        return None
    return Evidence(
        key,
        metric.label,
        _value(metrics, key),
        metric.unit,
        metric.status,
        observation,
    )


def _available_evidence(
    metrics: dict[str, MetricResult], *items: tuple[str, str]
) -> tuple[Evidence, ...]:
    return tuple(
        evidence
        for key, observation in items
        if (evidence := _evidence(metrics, key, observation)) is not None
    )


def _rating(
    *,
    key: str,
    label: str,
    stars: int,
    assessment: str,
    evidence: tuple[Evidence, ...],
    uncertainty: str,
) -> SuitabilityRating:
    return SuitabilityRating(
        key=key,
        label=label,
        stars=max(0, min(5, stars)),
        maximum_stars=5,
        assessment=assessment,
        evidence=evidence,
        uncertainty=uncertainty,
    )


def _web_rating(metrics: dict[str, MetricResult]) -> SuitabilityRating:
    dns = _value(metrics, "dns_ms")
    tcp = _value(metrics, "tcp_ms")
    ttfb = _value(metrics, "ttfb_ms")
    values = (dns, tcp, ttfb)
    evidence = _available_evidence(
        metrics,
        ("dns_ms", "이름 조회가 웹 탐색 시작 지연에 영향을 줍니다."),
        ("tcp_ms", "연결 설정 시간이 페이지 요청 전에 필요합니다."),
        ("ttfb_ms", "첫 응답 대기 시간이 페이지 체감 속도에 영향을 줍니다."),
    )
    if any(value is None for value in values):
        return _rating(
            key="web_browsing",
            label="웹 브라우징",
            stars=0,
            assessment="판단에 필요한 DNS·TCP·TTFB 측정값이 부족합니다.",
            evidence=evidence,
            uncertainty="누락된 지표를 다시 측정해야 웹 브라우징 적합도를 판단할 수 있습니다.",
        )
    stars = 5
    stars -= 2 if dns > 100 else 1 if dns > 50 else 0
    stars -= 2 if tcp > 200 else 1 if tcp > 100 else 0
    stars -= 2 if ttfb > 800 else 1 if ttfb > 300 else 0
    stars = max(1, stars)
    return _rating(
        key="web_browsing",
        label="웹 브라우징",
        stars=stars,
        assessment=(
            "일반적인 웹 탐색에 원활할 것으로 예상됩니다."
            if stars >= 4
            else "웹 페이지 시작이나 응답에서 지연을 느낄 수 있습니다."
        ),
        evidence=evidence,
        uncertainty="사이트 자체 처리 시간과 페이지 크기는 별도로 영향을 줍니다.",
    )


def _stream_rating(
    metrics: dict[str, MetricResult], *, key: str, label: str, thresholds: tuple[float, ...]
) -> SuitabilityRating:
    throughput = _value(metrics, "download_mbps")
    loss = _value(metrics, "packet_loss_percent")
    evidence = _available_evidence(
        metrics,
        ("download_mbps", "영상 비트레이트를 지속해서 받을 수 있는 처리율입니다."),
        ("packet_loss_percent", "손실은 재전송과 버퍼링 가능성을 높입니다."),
    )
    if throughput is None:
        return _rating(
            key=key,
            label=label,
            stars=0,
            assessment="다운로드 처리율이 없어 스트리밍 적합도를 판단할 수 없습니다.",
            evidence=evidence,
            uncertainty="허가된 다운로드 대상을 설정한 뒤 다시 측정해야 합니다.",
        )
    stars = 1 + sum(throughput >= threshold for threshold in thresholds)
    if loss is not None:
        stars -= 2 if loss >= 5 else 1 if loss >= 1 else 0
    stars = max(1, stars)
    return _rating(
        key=key,
        label=label,
        stars=stars,
        assessment=(
            f"{label}에 충분한 여유가 있을 것으로 예상됩니다."
            if stars >= 4
            else f"{label}에서 화질 저하나 버퍼링이 생길 수 있습니다."
        ),
        evidence=evidence,
        uncertainty=(
            "실제 서비스 비트레이트, Wi-Fi 변동, 측정 서버의 전송 제한에 따라 결과가 달라질 수 있습니다."
        ),
    )


def _conference_rating(metrics: dict[str, MetricResult]) -> SuitabilityRating:
    throughput = _value(metrics, "download_mbps")
    rtt = _value(metrics, "rtt_ms")
    loss = _value(metrics, "packet_loss_percent")
    evidence = _available_evidence(
        metrics,
        ("download_mbps", "수신 영상과 음성에 사용할 수 있는 처리율입니다."),
        ("rtt_ms", "왕복 지연은 대화의 응답성에 영향을 줍니다."),
        ("packet_loss_percent", "손실은 음성 끊김과 영상 품질 저하를 유발할 수 있습니다."),
    )
    if rtt is None or loss is None:
        return _rating(
            key="video_conference",
            label="화상회의",
            stars=0,
            assessment="RTT 또는 패킷 손실 값이 없어 화상회의 적합도를 판단할 수 없습니다.",
            evidence=evidence,
            uncertainty="ICMP가 차단된 환경에서는 실제 화상회의 테스트가 추가로 필요합니다.",
        )
    stars = 5
    stars -= 2 if rtt > 300 else 1 if rtt > 150 else 0
    stars -= 3 if loss >= 5 else 1 if loss >= 1 else 0
    if throughput is not None:
        stars -= 2 if throughput < 2 else 1 if throughput < 5 else 0
    stars = max(1, stars)
    return _rating(
        key="video_conference",
        label="화상회의",
        stars=stars,
        assessment=(
            "일반적인 화상회의에 원활할 것으로 예상됩니다."
            if stars >= 4
            else "대화 지연, 끊김 또는 화질 저하가 생길 수 있습니다."
        ),
        evidence=evidence,
        uncertainty="업로드 처리율과 지터를 아직 측정하지 않으므로 수신 측 관점의 추정입니다.",
    )


def analyze(result: RunResult) -> DiagnosticAnalysis:
    """Return deterministic diagnoses without changing the raw measurements."""

    metrics = _metric_map(result)
    dns = _value(metrics, "dns_ms")
    tcp = _value(metrics, "tcp_ms")
    ttfb = _value(metrics, "ttfb_ms")
    throughput = _value(metrics, "download_mbps")
    rtt = _value(metrics, "rtt_ms")
    loss = _value(metrics, "packet_loss_percent")
    findings: list[Finding] = []

    missing = [
        metrics[key].label if key in metrics else key
        for key in ("dns_ms", "tcp_ms", "ttfb_ms", "download_mbps", "rtt_ms")
        if _value(metrics, key) is None
    ]
    if missing:
        missing_evidence = _available_evidence(
            metrics,
            *(
                (key, "성공한 표본이 없어 이 지표를 분석에서 제외했습니다.")
                for key in ("dns_ms", "tcp_ms", "ttfb_ms", "download_mbps", "rtt_ms")
                if _value(metrics, key) is None
            ),
        )
        findings.append(
            Finding(
                "incomplete_measurement",
                "warning",
                "일부 지표를 판단할 수 없음",
                f"측정값이 없는 지표: {', '.join(missing)}",
                missing_evidence,
                "누락된 지표와 관련된 병목은 검출하지 못할 수 있습니다.",
            )
        )

    if dns is not None and dns > 100:
        findings.append(
            Finding(
                "dns_delay",
                "warning",
                "DNS 조회 지연",
                "도메인 이름을 주소로 바꾸는 단계가 길어 웹사이트 접속 시작이 늦어질 수 있습니다.",
                _available_evidence(metrics, ("dns_ms", "100 ms 기준을 초과했습니다.")),
                "DNS 캐시 상태와 재귀 DNS 서버 위치 때문에 실행마다 달라질 수 있습니다.",
            )
        )

    if loss is not None and loss >= 5:
        findings.append(
            Finding(
                "packet_loss",
                "critical",
                "패킷 손실이 큼",
                "재전송과 실시간 음성·영상 끊김 가능성이 큽니다.",
                _available_evidence(metrics, ("packet_loss_percent", "5% 기준 이상입니다.")),
                "ICMP 응답 우선순위가 실제 응용 트래픽과 다를 수 있습니다.",
            )
        )
    elif loss is not None and loss >= 1:
        findings.append(
            Finding(
                "packet_loss",
                "warning",
                "패킷 손실 관측",
                "일부 패킷 손실이 관측되어 실시간 서비스 품질에 영향을 줄 수 있습니다.",
                _available_evidence(metrics, ("packet_loss_percent", "1% 기준 이상입니다.")),
                "적은 표본의 ICMP 결과이므로 더 긴 시간의 재측정이 필요합니다.",
            )
        )

    if rtt is not None and throughput is not None and rtt > 100 and throughput < 10:
        findings.append(
            Finding(
                "latency_and_throughput",
                "critical" if rtt > 200 or throughput < 3 else "warning",
                "장거리 경로 또는 혼잡 가능성",
                "RTT가 높은 동시에 처리율이 낮아 긴 경로, 큐잉 혼잡 또는 손실의 영향을 의심할 수 있습니다.",
                _available_evidence(
                    metrics,
                    ("rtt_ms", "100 ms 기준을 초과했습니다."),
                    ("download_mbps", "10 Mbps 기준보다 낮습니다."),
                ),
                "이 조합만으로 장거리 경로와 혼잡을 서로 구분할 수는 없습니다.",
            )
        )
    elif rtt is not None and throughput is not None and rtt <= 50 and throughput < 10:
        findings.append(
            Finding(
                "low_throughput_near_path",
                "critical" if throughput < 3 else "warning",
                "액세스 링크 또는 서버 제한 가능성",
                "RTT는 낮지만 처리율이 낮아 로컬 접속 구간, Wi-Fi, 회선 또는 측정 서버 제한을 의심할 수 있습니다.",
                _available_evidence(
                    metrics,
                    ("rtt_ms", "50 ms 이하로 경로 지연은 낮습니다."),
                    ("download_mbps", "10 Mbps 기준보다 낮습니다."),
                ),
                "업로드·링크별 측정이 없어 액세스 링크와 서버 제한을 확정할 수 없습니다.",
            )
        )
    elif rtt is not None and rtt > 150:
        findings.append(
            Finding(
                "high_rtt",
                "warning",
                "RTT가 높음",
                "상호작용이 필요한 웹 요청과 실시간 대화에서 반응이 늦어질 수 있습니다.",
                _available_evidence(metrics, ("rtt_ms", "150 ms 기준을 초과했습니다.")),
                "대상 서버까지의 거리와 순간 큐잉 지연이 모두 포함된 값입니다.",
            )
        )

    if ttfb is not None and ttfb > 500:
        findings.append(
            Finding(
                "slow_server_response",
                "warning",
                "HTTP 응답 시작이 느림",
                "연결 뒤 첫 바이트를 받기까지 오래 걸려 서버 처리 또는 응답 경로 지연 가능성이 있습니다.",
                _available_evidence(
                    metrics,
                    ("ttfb_ms", "500 ms 기준을 초과했습니다."),
                    ("tcp_ms", "TCP 연결 시간과 비교할 수 있습니다."),
                ),
                "TTFB에는 대상 서버의 부하와 응용 처리 시간이 포함되어 네트워크만의 문제로 단정할 수 없습니다.",
            )
        )
    elif tcp is not None and tcp > 200:
        findings.append(
            Finding(
                "slow_tcp_connect",
                "warning",
                "TCP 연결 설정이 느림",
                "연결 설정 시간이 길어 새 연결을 자주 만드는 서비스가 느리게 느껴질 수 있습니다.",
                _available_evidence(metrics, ("tcp_ms", "200 ms 기준을 초과했습니다.")),
                "서버 거리, 방화벽, 순간 혼잡 중 어느 하나로 원인을 확정할 수 없습니다.",
            )
        )

    if not findings:
        findings.append(
            Finding(
                "no_major_bottleneck",
                "info",
                "뚜렷한 병목 없음",
                "현재 규칙의 경고 기준을 넘는 핵심 지표가 없습니다.",
                _available_evidence(
                    metrics,
                    ("dns_ms", "DNS 대표값입니다."),
                    ("rtt_ms", "RTT 대표값입니다."),
                    ("download_mbps", "다운로드 대표값입니다."),
                ),
                SINGLE_RUN_UNCERTAINTY,
            )
        )

    severities = {finding.severity for finding in findings}
    available_core = sum(
        _value(metrics, key) is not None
        for key in ("dns_ms", "tcp_ms", "ttfb_ms", "download_mbps", "rtt_ms")
    )
    if available_core < 3:
        overall_rating, overall_label = "unknown", "판단 보류"
        summary = "핵심 측정값이 부족해 현재 네트워크 상태를 종합 평가할 수 없습니다."
    elif "critical" in severities:
        overall_rating, overall_label = "poor", "나쁨"
        summary = "현재 측정에서는 체감 품질에 큰 영향을 줄 수 있는 문제가 관측되었습니다."
    elif "warning" in severities:
        overall_rating, overall_label = "fair", "보통"
        summary = "기본 사용은 가능하지만 일부 지표에서 품질 저하 가능성이 관측되었습니다."
    else:
        overall_rating, overall_label = "good", "좋음"
        summary = "현재 측정에서는 주요 지표가 일반적인 사용에 양호한 범위입니다."

    primary = next(
        (finding.title for finding in findings if finding.severity == "critical"),
        next(
            (finding.title for finding in findings if finding.severity == "warning"),
            "없음",
        ),
    )
    overall_evidence = _available_evidence(
        metrics,
        ("dns_ms", "이름 조회 대표값입니다."),
        ("rtt_ms", "왕복 지연 대표값입니다."),
        ("download_mbps", "처리율 대표값입니다."),
        ("packet_loss_percent", "패킷 손실 관측값입니다."),
    )

    return DiagnosticAnalysis(
        overall_rating=overall_rating,
        overall_label=overall_label,
        summary=summary,
        primary_bottleneck=primary,
        evidence=overall_evidence,
        findings=tuple(findings),
        suitability=(
            _web_rating(metrics),
            _stream_rating(
                metrics,
                key="streaming_1080p",
                label="1080p 스트리밍",
                thresholds=(3, 5, 8, 15),
            ),
            _stream_rating(
                metrics,
                key="streaming_4k",
                label="4K 스트리밍",
                thresholds=(5, 10, 15, 25),
            ),
            _conference_rating(metrics),
        ),
        uncertainty=SINGLE_RUN_UNCERTAINTY,
    )
