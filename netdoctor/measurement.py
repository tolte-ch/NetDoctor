"""Real network measurements for NetDoctor week two.

The functions in this module keep DNS, TCP, TLS, HTTP TTFB, body transfer, and
ICMP timing separate so one phase is not accidentally counted as another.
"""

from __future__ import annotations

import math
import platform
import queue
import re
import socket
import ssl
import subprocess
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Protocol
from urllib.parse import SplitResult, quote, urlsplit, urlunsplit
from uuid import uuid4

from .config import AppConfig
from .models import MetricResult, RunResult


class MeasurementError(RuntimeError):
    """A single network phase could not produce a valid measurement."""


@dataclass(frozen=True, slots=True)
class Endpoint:
    """A resolved TCP endpoint independent of DNS timing."""

    family: int
    ip: str
    port: int
    flowinfo: int = 0
    scope_id: int = 0

    @property
    def sockaddr(self) -> tuple[object, ...]:
        if self.family == socket.AF_INET6:
            return (self.ip, self.port, self.flowinfo, self.scope_id)
        return (self.ip, self.port)

    def with_port(self, port: int) -> Endpoint:
        return Endpoint(self.family, self.ip, port, self.flowinfo, self.scope_id)


class MeasurementBackend(Protocol):
    def resolve(self, host: str, port: int, timeout: float) -> tuple[float, tuple[Endpoint, ...]]:
        ...

    def tcp(self, endpoint: Endpoint, timeout: float) -> float:
        ...

    def http_ttfb(
        self, url: str, endpoint: Endpoint, timeout: float
    ) -> tuple[float, float | None, int]:
        ...

    def download(
        self, url: str, timeout: float, max_bytes: int, max_seconds: float
    ) -> float:
        ...

    def ping(self, host: str, timeout: float) -> float:
        ...


def _url_port(parts: SplitResult) -> int:
    if parts.port is not None:
        return parts.port
    return 443 if parts.scheme == "https" else 80


def _ascii_host(host: str) -> str:
    return host if ":" in host else host.encode("idna").decode("ascii")


def _ascii_url(url: str) -> str:
    parts = urlsplit(url)
    if not parts.hostname:
        raise MeasurementError("URL does not contain a host")
    if parts.username is not None or parts.password is not None:
        raise MeasurementError("URLs containing credentials are not allowed")
    host = _ascii_host(parts.hostname)
    host = f"[{host}]" if ":" in host else host
    netloc = host if parts.port is None else f"{host}:{parts.port}"
    path = quote(parts.path or "/", safe="/%:@!$&'()*+,;=-._~")
    query = quote(parts.query, safe="=&%:@!$()*+,;/?-._~")
    return urlunsplit((parts.scheme, netloc, path, query, ""))


def _endpoint_from_sockaddr(family: int, sockaddr: tuple[object, ...]) -> Endpoint:
    if family == socket.AF_INET6:
        return Endpoint(
            family=family,
            ip=str(sockaddr[0]),
            port=int(sockaddr[1]),
            flowinfo=int(sockaddr[2]),
            scope_id=int(sockaddr[3]),
        )
    return Endpoint(family=family, ip=str(sockaddr[0]), port=int(sockaddr[1]))


def parse_ping_rtt(output: str) -> float:
    """Extract one RTT from common localized ping output."""

    match = re.search(r"([=<])\s*([0-9]+(?:[.,][0-9]+)?)\s*ms", output, re.IGNORECASE)
    if match is None:
        raise MeasurementError("ping output did not contain an RTT")
    value = float(match.group(2).replace(",", "."))
    return 0.5 if match.group(1) == "<" and value <= 1 else value


class NetworkMeasurer:
    """Standard-library implementation of the network phases."""

    def resolve(self, host: str, port: int, timeout: float) -> tuple[float, tuple[Endpoint, ...]]:
        result_queue: queue.Queue[object] = queue.Queue(maxsize=1)

        def worker() -> None:
            try:
                result_queue.put(socket.getaddrinfo(host, port, type=socket.SOCK_STREAM))
            except OSError as error:
                result_queue.put(error)

        started = time.perf_counter()
        thread = threading.Thread(target=worker, name="netdoctor-dns", daemon=True)
        thread.start()
        try:
            result = result_queue.get(timeout=timeout)
        except queue.Empty as error:
            raise MeasurementError(f"DNS lookup timed out after {timeout:g}s") from error
        elapsed_ms = (time.perf_counter() - started) * 1_000

        if isinstance(result, OSError):
            raise MeasurementError(f"DNS lookup failed: {result}") from result

        endpoints: list[Endpoint] = []
        seen: set[tuple[object, ...]] = set()
        for family, socktype, _protocol, _canonname, sockaddr in result:  # type: ignore[union-attr]
            if socktype != socket.SOCK_STREAM or family not in {socket.AF_INET, socket.AF_INET6}:
                continue
            endpoint = _endpoint_from_sockaddr(family, sockaddr)
            key = (endpoint.family, *endpoint.sockaddr)
            if key not in seen:
                seen.add(key)
                endpoints.append(endpoint)
        if not endpoints:
            raise MeasurementError("DNS lookup returned no TCP endpoints")
        return elapsed_ms, tuple(endpoints)

    def tcp(self, endpoint: Endpoint, timeout: float) -> float:
        started = time.perf_counter()
        try:
            with socket.socket(endpoint.family, socket.SOCK_STREAM) as connection:
                connection.settimeout(timeout)
                connection.connect(endpoint.sockaddr)
        except OSError as error:
            raise MeasurementError(f"TCP connection failed: {error}") from error
        return (time.perf_counter() - started) * 1_000

    def http_ttfb(
        self, url: str, endpoint: Endpoint, timeout: float
    ) -> tuple[float, float | None, int]:
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise MeasurementError("TTFB URL must be an absolute HTTP(S) URL")

        port = _url_port(parts)
        target = endpoint.with_port(port)
        connection: socket.socket | ssl.SSLSocket
        tls_ms: float | None = None
        try:
            raw = socket.socket(target.family, socket.SOCK_STREAM)
            raw.settimeout(timeout)
            raw.connect(target.sockaddr)
            connection = raw
            if parts.scheme == "https":
                tls_started = time.perf_counter()
                context = ssl.create_default_context()
                connection = context.wrap_socket(raw, server_hostname=parts.hostname)
                tls_ms = (time.perf_counter() - tls_started) * 1_000

            request_target = quote(parts.path or "/", safe="/%:@!$&'()*+,;=-._~")
            if parts.query:
                request_target += f"?{quote(parts.query, safe='=&%:@!$()*+,;/?-._~')}"
            default_port = 443 if parts.scheme == "https" else 80
            ascii_host = _ascii_host(parts.hostname)
            host_name = f"[{ascii_host}]" if ":" in ascii_host else ascii_host
            host_header = host_name if port == default_port else f"{host_name}:{port}"
            request = (
                f"GET {request_target} HTTP/1.1\r\n"
                f"Host: {host_header}\r\n"
                "User-Agent: NetDoctor/0.2\r\n"
                "Accept: */*\r\n"
                "Accept-Encoding: identity\r\n"
                "Connection: close\r\n\r\n"
            ).encode("ascii")

            started = time.perf_counter()
            connection.sendall(request)
            first_byte = connection.recv(1)
            ttfb_ms = (time.perf_counter() - started) * 1_000
            if not first_byte:
                raise MeasurementError("HTTP server closed before sending a response")

            header = bytearray(first_byte)
            while b"\r\n\r\n" not in header and len(header) < 65_536:
                chunk = connection.recv(4_096)
                if not chunk:
                    break
                header.extend(chunk)
            status_line = bytes(header).split(b"\r\n", 1)[0]
            match = re.match(rb"HTTP/\d(?:\.\d)?\s+(\d{3})", status_line)
            if match is None:
                raise MeasurementError("server did not return a valid HTTP status line")
            return ttfb_ms, tls_ms, int(match.group(1))
        except (OSError, ssl.SSLError) as error:
            raise MeasurementError(f"HTTP TTFB measurement failed: {error}") from error
        finally:
            if "connection" in locals():
                connection.close()
            elif "raw" in locals():
                raw.close()

    def download(
        self, url: str, timeout: float, max_bytes: int, max_seconds: float
    ) -> float:
        request = urllib.request.Request(
            _ascii_url(url),
            headers={"User-Agent": "NetDoctor/0.2", "Accept-Encoding": "identity"},
        )
        operation_timeout = min(timeout, max_seconds)
        try:
            with urllib.request.urlopen(request, timeout=operation_timeout) as response:
                first_byte = response.read(1)
                if not first_byte:
                    raise MeasurementError("download response body was empty")
                transferred = 0
                started = time.perf_counter()
                deadline = started + max_seconds
                while transferred < max_bytes and time.perf_counter() < deadline:
                    chunk = response.read(min(65_536, max_bytes - transferred))
                    if not chunk:
                        break
                    transferred += len(chunk)
                elapsed = time.perf_counter() - started
        except MeasurementError:
            raise
        except (OSError, ValueError) as error:
            raise MeasurementError(f"download failed: {error}") from error

        if transferred == 0 or elapsed <= 0:
            raise MeasurementError("download body was too small to measure throughput")
        return transferred * 8 / elapsed / 1_000_000

    def ping(self, host: str, timeout: float) -> float:
        system = platform.system()
        if system == "Windows":
            command = ["ping", "-n", "1", "-w", str(max(1, round(timeout * 1_000))), host]
        elif system == "Darwin":
            command = ["ping", "-c", "1", "-W", str(max(1, round(timeout * 1_000))), host]
        else:
            command = ["ping", "-c", "1", "-W", str(max(1, math.ceil(timeout))), host]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=timeout + 1,
                check=False,
            )
        except FileNotFoundError as error:
            raise MeasurementError("ping command is unavailable") from error
        except subprocess.TimeoutExpired as error:
            raise MeasurementError(f"ping timed out after {timeout:g}s") from error
        output = f"{completed.stdout}\n{completed.stderr}"
        try:
            return parse_ping_rtt(output)
        except MeasurementError as error:
            detail = "destination did not answer" if completed.returncode else str(error)
            raise MeasurementError(f"ping failed: {detail}") from error


@dataclass(slots=True)
class _Accumulator:
    key: str
    label: str
    unit: str
    samples: list[float] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    unavailable_reason: str | None = None

    def success(self, value: float) -> None:
        self.samples.append(round(float(value), 3))

    def failure(self, error: Exception | str) -> None:
        self.errors.append(str(error))

    def finish(self, repetitions: int) -> MetricResult:
        samples = tuple(self.samples)
        if samples and self.errors:
            error = f"{len(self.errors)}/{repetitions} attempts failed; last: {self.errors[-1]}"
            return MetricResult(self.key, self.label, self.unit, samples, "partial", error)
        if samples:
            return MetricResult(self.key, self.label, self.unit, samples)
        if self.unavailable_reason:
            return MetricResult(
                self.key,
                self.label,
                self.unit,
                (),
                "unavailable",
                self.unavailable_reason,
            )
        error = self.errors[-1] if self.errors else "measurement produced no samples"
        return MetricResult(self.key, self.label, self.unit, (), "failed", error)


ProgressCallback = Callable[[int, int], None]


def run_diagnostics(
    config: AppConfig,
    *,
    backend: MeasurementBackend | None = None,
    progress: ProgressCallback | None = None,
) -> RunResult:
    """Run all configured measurements while keeping partial failures isolated."""

    measurer = backend or NetworkMeasurer()
    started_at = datetime.now(timezone.utc).isoformat()
    dns = _Accumulator("dns_ms", "DNS", "ms")
    tcp = _Accumulator("tcp_ms", "TCP 연결", "ms")
    tls = _Accumulator("tls_ms", "TLS 연결", "ms")
    ttfb = _Accumulator("ttfb_ms", "TTFB", "ms")
    download = _Accumulator("download_mbps", "다운로드", "Mbps")
    rtt = _Accumulator("rtt_ms", "RTT", "ms")
    ping_failures = 0
    notes: list[str] = []

    http_parts = urlsplit(config.target.http_url)
    http_host = http_parts.hostname or config.target.host
    http_port = _url_port(http_parts)
    if config.target.download_url is None:
        download.unavailable_reason = "download_url is not configured"

    for index in range(config.repetitions):
        if progress:
            progress(index + 1, config.repetitions)

        endpoints: tuple[Endpoint, ...] = ()
        try:
            dns_ms, endpoints = measurer.resolve(
                config.target.host,
                config.target.tcp_port,
                config.timeout_seconds,
            )
            dns.success(dns_ms)
        except MeasurementError as error:
            dns.failure(error)

        if endpoints:
            tcp_error: MeasurementError | None = None
            for endpoint in endpoints:
                try:
                    tcp.success(measurer.tcp(endpoint, config.timeout_seconds))
                    break
                except MeasurementError as error:
                    tcp_error = error
            else:
                tcp.failure(tcp_error or "all TCP endpoints failed")
        else:
            tcp.failure("TCP skipped because DNS returned no endpoint")

        http_endpoints = endpoints
        same_target = (
            http_host.casefold() == config.target.host.casefold()
            and http_port == config.target.tcp_port
        )
        if not same_target:
            try:
                _ignored_ms, http_endpoints = measurer.resolve(
                    http_host,
                    http_port,
                    config.timeout_seconds,
                )
            except MeasurementError as error:
                http_endpoints = ()
                ttfb.failure(f"HTTP host lookup failed: {error}")
                tls.failure(f"HTTP host lookup failed: {error}")

        if http_endpoints:
            http_error: MeasurementError | None = None
            for endpoint in http_endpoints:
                try:
                    ttfb_ms, tls_ms, status_code = measurer.http_ttfb(
                        config.target.http_url,
                        endpoint,
                        config.timeout_seconds,
                    )
                    ttfb.success(ttfb_ms)
                    if tls_ms is None:
                        tls.unavailable_reason = "HTTP target does not use TLS"
                    else:
                        tls.success(tls_ms)
                    if not 200 <= status_code < 400:
                        notes.append(
                            f"HTTP target returned status {status_code} on run {index + 1}"
                        )
                    break
                except MeasurementError as error:
                    http_error = error
            else:
                ttfb.failure(http_error or "all HTTP endpoints failed")
                if http_parts.scheme == "https":
                    tls.failure(http_error or "all TLS endpoints failed")
        elif same_target:
            ttfb.failure("TTFB skipped because DNS returned no endpoint")
            if http_parts.scheme == "https":
                tls.failure("TLS skipped because DNS returned no endpoint")

        if config.target.download_url is not None:
            try:
                download.success(
                    measurer.download(
                        config.target.download_url,
                        config.timeout_seconds,
                        config.download_max_bytes,
                        config.download_max_seconds,
                    )
                )
            except MeasurementError as error:
                download.failure(error)

        try:
            rtt.success(measurer.ping(config.target.host, config.timeout_seconds))
        except MeasurementError as error:
            ping_failures += 1
            rtt.failure(error)

        if index + 1 < config.repetitions and config.pause_between_runs_seconds:
            time.sleep(config.pause_between_runs_seconds)

    metrics = [
        dns.finish(config.repetitions),
        tcp.finish(config.repetitions),
        tls.finish(config.repetitions),
        ttfb.finish(config.repetitions),
        download.finish(config.repetitions),
        rtt.finish(config.repetitions),
    ]
    if rtt.samples:
        loss = round(ping_failures / config.repetitions * 100, 3)
        metrics.append(MetricResult("packet_loss_percent", "패킷 손실", "%", (loss,)))
    else:
        metrics.append(
            MetricResult(
                "packet_loss_percent",
                "패킷 손실",
                "%",
                (),
                "unavailable",
                "all ping attempts failed; ICMP blocking cannot be distinguished from loss",
            )
        )

    if http_host.casefold() != config.target.host.casefold():
        notes.append("HTTP URL uses a different host; its DNS time is not included in dns_ms")

    return RunResult(
        schema_version="1.1",
        run_id=str(uuid4()),
        mode="measured",
        target_name=config.target.name,
        target_host=config.target.host,
        repetitions_requested=config.repetitions,
        started_at=started_at,
        finished_at=datetime.now(timezone.utc).isoformat(),
        metrics=tuple(metrics),
        notes=tuple(dict.fromkeys(notes)),
    )
