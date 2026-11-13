"""Small loopback-only HTTP server for manual NetDoctor testing."""

from __future__ import annotations

import argparse
import http.server


DOWNLOAD_BYTES = 4_000_000
CHUNK = b"N" * 65_536


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "NetDoctorLocalTest/1.0"

    def do_GET(self) -> None:
        if self.path == "/download":
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(DOWNLOAD_BYTES))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            remaining = DOWNLOAD_BYTES
            try:
                while remaining:
                    block = CHUNK[:remaining]
                    self.wfile.write(block)
                    remaining -= len(block)
            except (BrokenPipeError, ConnectionResetError):
                # NetDoctor intentionally stops after its configured byte/time limit.
                pass
            return

        body = b"NetDoctor local measurement target\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, message: str, *args: object) -> None:
        print(f"{self.address_string()} - {message % args}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Loopback target for NetDoctor tests")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if not 1 <= args.port <= 65_535:
        parser.error("port must be between 1 and 65535")

    server = http.server.ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Local NetDoctor target: http://127.0.0.1:{args.port}")
    print("Stop with Ctrl+C.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nLocal target stopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
