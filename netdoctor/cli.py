"""Command-line interface for NetDoctor."""

from __future__ import annotations

import argparse
import functools
import http.server
import sys
from dataclasses import replace
from pathlib import Path
from typing import Sequence

from .analysis import analyze
from .config import AppConfig, load_config
from .measurement import run_diagnostics
from .models import RunResult


METRIC_PLAN = (
    ("DNS", "호스트 이름을 IP 주소로 변환하는 시간"),
    ("TCP", "미리 확인한 IP 주소와 TCP 연결을 설정하는 시간"),
    ("TTFB", "연결 후 HTTP 요청을 보내 첫 응답 바이트를 받는 시간"),
    ("다운로드", "응답 본문 비트 수를 실제 전송 시간으로 나눈 처리율"),
    ("RTT", "ping 요청이 목적지를 왕복하는 시간과 패킷 손실률"),
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="netdoctor",
        description="Explainable network quality diagnostics",
    )
    parser.add_argument("--config", help="JSON configuration path")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("plan", help="show the validated measurement plan")
    demo = commands.add_parser("demo", help="show fictional data for contract validation")
    demo.add_argument("--json", action="store_true", help="emit the full JSON result")
    measure = commands.add_parser("measure", help="run real network measurements")
    measure.add_argument("--json", action="store_true", help="emit the full JSON result")
    measure.add_argument("--output", type=Path, help="also save the result as UTF-8 JSON")
    measure.add_argument(
        "--quiet",
        action="store_true",
        help="hide per-run progress messages",
    )
    dashboard = commands.add_parser("dashboard", help="serve the dashboard wireframe")
    dashboard.add_argument("--host", default="127.0.0.1")
    dashboard.add_argument("--port", type=int, default=8000)
    return parser


def _print_plan(config: AppConfig) -> None:
    print("NetDoctor 측정 계획")
    print(f"대상: {config.target.name} ({config.target.host}:{config.target.tcp_port})")
    print(f"HTTP: {config.target.http_url}")
    download = config.target.download_url or "미설정 - 허가된 측정 서버 필요"
    print(f"다운로드: {download}")
    print(f"반복: {config.repetitions}회 / 타임아웃: {config.timeout_seconds:g}초")
    print()
    for index, (name, definition) in enumerate(METRIC_PLAN, start=1):
        print(f"{index}. {name}: {definition}")
    print("\n이 명령은 계획만 표시하며 네트워크 요청을 보내지 않습니다.")


def _print_demo(result: RunResult) -> None:
    print("NetDoctor 데모 결과 - 실제 측정값이 아닙니다")
    print(f"대상: {result.target_name} ({result.target_host})")
    for metric in result.metrics:
        assert metric.summary is not None
        summary = metric.summary
        print(
            f"{metric.label:<8} 중앙값 {summary.median:>6.1f} {metric.unit} "
            f"(최소 {summary.minimum:.1f}, 최대 {summary.maximum:.1f})"
        )
    print(f"\n주의: {result.notes[0]}")
    _print_analysis(result)


def _stars(value: int, maximum: int) -> str:
    return "★" * value + "☆" * (maximum - value)


def _print_analysis(result: RunResult) -> None:
    if result.analysis is None:
        return
    analysis = result.analysis
    print(f"\n종합 평가: {analysis.overall_label}")
    print(f"주요 병목 추정: {analysis.primary_bottleneck}")
    print(analysis.summary)
    for rating in analysis.suitability:
        stars = _stars(rating.stars, rating.maximum_stars)
        print(f"{rating.label:<14} {stars} - {rating.assessment}")
    print("\n판정 근거:")
    for finding in analysis.findings:
        values = ", ".join(
            f"{item.label} {item.value:.2f} {item.unit}"
            if item.value is not None
            else f"{item.label} {item.status}"
            for item in finding.evidence
        ) or "해당 지표가 결과에 없음"
        print(f"- {finding.title}: {finding.explanation} [{values}]")
        print(f"  불확실성: {finding.uncertainty}")
    print(f"전체 한계: {analysis.uncertainty}")


def _print_measured(result: RunResult) -> None:
    print("NetDoctor 실제 측정 결과")
    print(f"대상: {result.target_name} ({result.target_host})")
    print(f"반복 요청: {result.repetitions_requested}회")
    for metric in result.metrics:
        if metric.summary is None:
            print(f"{metric.label:<9} {metric.status:>11} - {metric.error or '측정값 없음'}")
            continue
        summary = metric.summary
        detail = (
            f"중앙값 {summary.median:>8.2f} {metric.unit} "
            f"(평균 {summary.mean:.2f}, 최소 {summary.minimum:.2f}, "
            f"최대 {summary.maximum:.2f}, n={summary.count})"
        )
        print(f"{metric.label:<9} {metric.status:>11} - {detail}")
        if metric.error:
            print(f"{'':<22}주의: {metric.error}")
    for note in result.notes:
        print(f"참고: {note}")
    _print_analysis(result)


def _serve_dashboard(host: str, port: int) -> int:
    if not 1 <= port <= 65535:
        raise ValueError("port must be between 1 and 65535")
    web_root = Path(__file__).resolve().parent / "web"
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(web_root))
    try:
        server = http.server.ThreadingHTTPServer((host, port), handler)
    except OSError as error:
        raise ValueError(f"could not start dashboard server: {error}") from error
    print(f"NetDoctor dashboard: http://{host}:{port}")
    print("종료하려면 Ctrl+C를 누르세요.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard stopped.")
    finally:
        server.server_close()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        config = load_config(args.config)
        if args.command == "plan":
            _print_plan(config)
        elif args.command == "demo":
            result = RunResult.demo(
                config.target.name,
                config.target.host,
                config.repetitions,
            )
            result = replace(result, analysis=analyze(result), schema_version="1.2")
            if args.json:
                print(result.to_json())
            else:
                _print_demo(result)
        elif args.command == "measure":
            if args.config is None:
                raise ValueError(
                    "real measurement requires --config with a target you are authorized to test"
                )

            def progress(current: int, total: int) -> None:
                if not args.quiet:
                    print(f"측정 {current}/{total} 실행 중...", file=sys.stderr)

            result = run_diagnostics(config, progress=progress)
            result = replace(result, analysis=analyze(result), schema_version="1.2")
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(result.to_json() + "\n", encoding="utf-8")
            if args.json:
                print(result.to_json())
            else:
                _print_measured(result)
        elif args.command == "dashboard":
            return _serve_dashboard(args.host, args.port)
    except ValueError as error:
        print(f"설정 오류: {error}", file=sys.stderr)
        return 2
    return 0
