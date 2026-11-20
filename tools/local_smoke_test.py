"""Run the local HTTP target and NetDoctor inside one Python process.

This is useful in sandboxed environments that allow loopback sockets inside a
process but block connections between two separately launched processes.
"""

from __future__ import annotations

import argparse
import http.server
import sys
import threading
from dataclasses import replace
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from local_test_server import Handler
from netdoctor.analysis import analyze
from netdoctor.cli import _print_measured
from netdoctor.config import AppConfig, TargetConfig
from netdoctor.measurement import run_diagnostics


class QuietHandler(Handler):
    """Keep HTTP access logs from corrupting optional JSON output."""

    def log_message(self, message: str, *args: object) -> None:
        pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="run a single-process NetDoctor local smoke test"
    )
    parser.add_argument("--json", action="store_true", help="print the full JSON result")
    parser.add_argument("--output", type=Path, help="also save the result as UTF-8 JSON")
    return parser


def main() -> int:
    args = _parser().parse_args()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), QuietHandler)
    port = int(server.server_address[1])
    thread = threading.Thread(
        target=server.serve_forever,
        name="netdoctor-local-target",
        daemon=True,
    )
    thread.start()

    config = AppConfig(
        repetitions=5,
        timeout_seconds=2.0,
        pause_between_runs_seconds=0.1,
        target=TargetConfig(
            name="Single-process local test server",
            host="127.0.0.1",
            tcp_port=port,
            http_url=f"http://127.0.0.1:{port}/",
            download_url=f"http://127.0.0.1:{port}/download",
        ),
        download_max_bytes=2_000_000,
        download_max_seconds=2.0,
    )

    try:
        print(f"단일 프로세스 로컬 서버: 127.0.0.1:{port}", file=sys.stderr)
        result = run_diagnostics(
            config,
            progress=lambda current, total: print(
                f"측정 {current}/{total} 실행 중...", file=sys.stderr
            ),
        )
        result = replace(result, analysis=analyze(result), schema_version="1.2")
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(result.to_json() + "\n", encoding="utf-8")
        if args.json:
            print(result.to_json())
        else:
            _print_measured(result)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
