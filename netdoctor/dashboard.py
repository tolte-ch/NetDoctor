"""Dashboard web server: static page plus a small JSON measurement API.

Only the standard library is used so the project still runs without installing
packages. The measurement target is fixed by the server's ``--config`` file; the
browser can only choose the repetition count, never the host, so the dashboard
cannot be used to measure servers the operator has not authorized.
"""

from __future__ import annotations

import functools
import http.server
import json
import threading
from dataclasses import replace
from http import HTTPStatus
from pathlib import Path
from typing import Any, Callable

from .config import AppConfig
from .measurement import run_diagnostics
from .models import RunResult


WEB_ROOT = Path(__file__).resolve().parent / "web"
MAX_REQUEST_BYTES = 4_096

MeasureFunction = Callable[[AppConfig], RunResult]


class DashboardState:
    """Configuration and a lock shared by every request handler thread."""

    def __init__(
        self,
        config: AppConfig,
        *,
        measurement_enabled: bool,
        measure: MeasureFunction = run_diagnostics,
    ) -> None:
        self.config = config
        self.measurement_enabled = measurement_enabled
        self.measure = measure
        self.lock = threading.Lock()
        self.last_result: RunResult | None = None

    def public_config(self) -> dict[str, Any]:
        target = self.config.target
        return {
            "measurement_enabled": self.measurement_enabled,
            "busy": self.lock.locked(),
            "repetitions": self.config.repetitions,
            "repetition_choices": list(range(5, 11)),
            "target": {
                "name": target.name,
                "host": target.host,
                "tcp_port": target.tcp_port,
                "http_url": target.http_url,
                "download_configured": target.download_url is not None,
            },
        }


class DashboardHandler(http.server.SimpleHTTPRequestHandler):
    """Serves ``netdoctor/web`` and the ``/api/*`` endpoints."""

    server_version = "NetDoctorDashboard/0.3"

    def __init__(self, *args: Any, state: DashboardState, **kwargs: Any) -> None:
        self.state = state
        super().__init__(*args, directory=str(WEB_ROOT), **kwargs)

    # ----- routing -------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 - http.server naming
        path = self.path.split("?", 1)[0]
        if path == "/api/config":
            self._send_json(HTTPStatus.OK, self.state.public_config())
        elif path == "/api/last":
            result = self.state.last_result
            if result is None:
                self._send_error(HTTPStatus.NOT_FOUND, "아직 측정 결과가 없습니다.")
            else:
                self._send_json(HTTPStatus.OK, result.to_dict())
        elif path.startswith("/api/"):
            self._send_error(HTTPStatus.NOT_FOUND, "알 수 없는 API 경로입니다.")
        else:
            super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/api/measure":
            self._handle_measure()
        else:
            self._send_error(HTTPStatus.NOT_FOUND, "알 수 없는 API 경로입니다.")

    # ----- /api/measure --------------------------------------------------
    def _handle_measure(self) -> None:
        if not self.state.measurement_enabled:
            self._send_error(
                HTTPStatus.FORBIDDEN,
                "실제 측정이 꺼져 있습니다. 허가된 대상을 담은 --config로 대시보드를 실행하세요.",
            )
            return

        # Requiring a JSON content type forces a CORS preflight for cross-site
        # requests, which this server never approves. Other websites therefore
        # cannot silently trigger measurements through the user's browser.
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip()
        if content_type != "application/json":
            self._send_error(
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "Content-Type must be application/json"
            )
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if not 0 <= length <= MAX_REQUEST_BYTES:
            self._send_error(HTTPStatus.BAD_REQUEST, "요청 본문 크기가 올바르지 않습니다.")
            return
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_error(HTTPStatus.BAD_REQUEST, "요청 본문이 올바른 JSON이 아닙니다.")
            return
        if not isinstance(body, dict):
            self._send_error(HTTPStatus.BAD_REQUEST, "요청 본문은 JSON 객체여야 합니다.")
            return

        repetitions = body.get("repetitions", self.state.config.repetitions)
        if isinstance(repetitions, bool) or not isinstance(repetitions, int):
            self._send_error(HTTPStatus.BAD_REQUEST, "repetitions는 정수여야 합니다.")
            return
        try:
            config = replace(self.state.config, repetitions=repetitions)
        except ValueError as error:
            self._send_error(HTTPStatus.BAD_REQUEST, str(error))
            return

        if not self.state.lock.acquire(blocking=False):
            self._send_error(HTTPStatus.CONFLICT, "이미 측정이 진행 중입니다. 잠시 후 다시 시도하세요.")
            return
        try:
            result = self.state.measure(config)
            self.state.last_result = result
        except Exception as error:  # keep the server alive and report the cause
            self.log_error("measurement crashed: %r", error)
            self._send_error(HTTPStatus.INTERNAL_SERVER_ERROR, f"측정 중 오류: {error}")
            return
        finally:
            self.state.lock.release()
        self._send_json(HTTPStatus.OK, result.to_dict())

    # ----- helpers -------------------------------------------------------
    def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, status: HTTPStatus, message: str) -> None:
        self._send_json(status, {"error": message})

    def end_headers(self) -> None:
        # The page changes while we develop it; avoid stale cached copies.
        if not self.path.startswith("/api/"):
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()


def create_server(
    host: str, port: int, state: DashboardState
) -> http.server.ThreadingHTTPServer:
    handler = functools.partial(DashboardHandler, state=state)
    return http.server.ThreadingHTTPServer((host, port), handler)


def serve(host: str, port: int, state: DashboardState) -> int:
    if not 1 <= port <= 65535:
        raise ValueError("port must be between 1 and 65535")
    try:
        server = create_server(host, port, state)
    except OSError as error:
        raise ValueError(f"could not start dashboard server: {error}") from error

    print(f"NetDoctor dashboard: http://{host}:{port}")
    if state.measurement_enabled:
        target = state.config.target
        print(f"측정 대상: {target.name} ({target.host}:{target.tcp_port})")
    else:
        print("실제 측정 꺼짐: --config 없이 실행했습니다. 화면만 확인할 수 있습니다.")
    if host not in {"127.0.0.1", "localhost", "::1"}:
        print("주의: 같은 네트워크의 다른 기기도 이 대시보드에서 측정을 시작할 수 있습니다.")
    print("종료하려면 Ctrl+C를 누르세요.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard stopped.")
    finally:
        server.server_close()
    return 0
