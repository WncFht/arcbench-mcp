"""TLS-compatible HTTP transport; never retry an ambiguous write."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

import httpx


class CurlTransport(httpx.BaseTransport):
    """Use system curl with verified TLS and credentials outside process arguments."""

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if not shutil.which("curl"):
            raise httpx.ConnectError("curl is unavailable", request=request)
        body = request.read()
        with tempfile.TemporaryDirectory(prefix="arcbench-http-") as folder:
            root = Path(folder)
            headers = root / "headers"
            headers.write_text("".join(f"{k}: {v}\n" for k, v in request.headers.items()))
            headers.chmod(0o600)
            timeouts = request.extensions.get("timeout", {})
            timeout = max((v for v in timeouts.values() if isinstance(v, (int, float))), default=60)
            args = [
                "curl", "--silent", "--show-error", "--max-time", str(timeout),
                "--request", request.method, "--header", f"@{headers}",
                "--dump-header", str(root / "response-headers"),
                "--output", str(root / "body"), "--write-out", "%{http_code}",
                "--url", str(request.url),
            ]
            if body:
                args += ["--data-binary", "@-"]
            try:
                result = subprocess.run(args, input=body, capture_output=True, timeout=timeout + 5)
            except subprocess.TimeoutExpired:
                raise httpx.ReadTimeout("curl request timed out; check server state before retrying", request=request) from None
            if result.returncode:
                # Do not expose stderr, headers or request bodies containing credentials.
                raise httpx.TransportError(f"curl failed (exit {result.returncode}); check server state before retrying", request=request)
            response_headers: list[tuple[str, str]] = []
            for line in (root / "response-headers").read_text().splitlines():
                if line.startswith("HTTP/"):
                    response_headers = []
                elif ":" in line:
                    k, v = line.split(":", 1)
                    response_headers.append((k, v.strip()))
            return httpx.Response(int(result.stdout), headers=response_headers,
                                  content=(root / "body").read_bytes(), request=request)


class CompatibleTransport(httpx.BaseTransport):
    """Fallback only on connection setup failure, before an HTTP request is sent."""

    def __init__(self) -> None:
        self.primary = httpx.HTTPTransport()
        self.fallback = CurlTransport()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        request.read()  # Buffer multipart streams so connection setup fallback can replay them.
        try:
            return self.primary.handle_request(request)
        except httpx.ConnectError:
            return self.fallback.handle_request(request)

    def close(self) -> None:
        self.primary.close()
        self.fallback.close()
