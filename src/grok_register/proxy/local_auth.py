"""Local HTTP CONNECT forwarder that injects Proxy-Authorization for Chromium.

Chromium --proxy-server cannot embed user:pass. For residential gateways like
  http://USER:PASS@proxy.example:2261
we bind 127.0.0.1:<port> and strip credentials into the Proxy-Authorization header
while forwarding CONNECT / absolute-form requests upstream.

One local listener per unique upstream URL (process-wide).
"""

from __future__ import annotations

import base64
import select
import socket
import socketserver
import threading
from typing import Any
from urllib.parse import urlparse

_lock = threading.Lock()
# upstream_url -> {"port": int, "server": ThreadingTCPServer, "thread": Thread}
_bridges: dict[str, dict[str, Any]] = {}


def _parse_upstream(proxy_url: str) -> tuple[str, int, str | None]:
    """Return (host, port, basic_auth_header_or_None)."""
    p = (proxy_url or "").strip()
    if not p:
        raise ValueError("empty proxy")
    u = urlparse(p if "://" in p else f"http://{p}")
    host = u.hostname or ""
    if not host:
        raise ValueError(f"bad proxy host: {proxy_url!r}")
    port = int(u.port or (443 if (u.scheme or "http") == "https" else 80))
    auth = None
    if u.username is not None:
        user = u.username
        passwd = u.password or ""
        # urlparse may leave percent-encoding
        from urllib.parse import unquote

        user = unquote(user)
        passwd = unquote(passwd)
        token = base64.b64encode(f"{user}:{passwd}".encode()).decode("ascii")
        auth = f"Basic {token}"
    return host, port, auth


class _Handler(socketserver.BaseRequestHandler):
    upstream_host: str = ""
    upstream_port: int = 0
    proxy_auth: str | None = None

    def handle(self) -> None:
        try:
            self.request.settimeout(30)
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = self.request.recv(4096)
                if not chunk:
                    return
                data += chunk
                if len(data) > 65536:
                    return
            head, _, rest = data.partition(b"\r\n\r\n")
            lines = head.split(b"\r\n")
            if not lines:
                return
            req_line = lines[0].decode("latin-1", errors="replace")
            parts = req_line.split()
            if len(parts) < 2:
                return
            method = parts[0].upper()
            target = parts[1]

            # Open upstream TCP
            up = socket.create_connection((self.upstream_host, self.upstream_port), timeout=20)
            up.settimeout(30)

            if method == "CONNECT":
                # Rebuild CONNECT with Proxy-Authorization
                out_lines = [f"CONNECT {target} HTTP/1.1".encode()]
                host_hdr = target.encode()
                out_lines.append(b"Host: " + host_hdr)
                if self.proxy_auth:
                    out_lines.append(
                        b"Proxy-Authorization: " + self.proxy_auth.encode("ascii")
                    )
                out_lines.append(b"Proxy-Connection: keep-alive")
                out_lines.append(b"Connection: keep-alive")
                out_lines.append(b"")
                out_lines.append(b"")
                up.sendall(b"\r\n".join(out_lines))
                # Read upstream response
                resp = b""
                while b"\r\n\r\n" not in resp:
                    chunk = up.recv(4096)
                    if not chunk:
                        break
                    resp += chunk
                self.request.sendall(resp if resp else b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                if b" 200 " not in resp.split(b"\r\n", 1)[0] and not resp.startswith(
                    b"HTTP/1.1 200"
                ) and not resp.startswith(b"HTTP/1.0 200"):
                    up.close()
                    return
                # Tunnel leftover + bidirectional
                if rest:
                    up.sendall(rest)
                self._tunnel(self.request, up)
            else:
                # Absolute-form HTTP proxy request
                headers = []
                for line in lines[1:]:
                    low = line.lower()
                    if low.startswith(b"proxy-authorization:"):
                        continue
                    if low.startswith(b"proxy-connection:"):
                        continue
                    headers.append(line)
                out = [lines[0]]
                if self.proxy_auth:
                    out.append(
                        b"Proxy-Authorization: " + self.proxy_auth.encode("ascii")
                    )
                out.append(b"Proxy-Connection: keep-alive")
                out.extend(headers)
                out.append(b"")
                out.append(b"")
                up.sendall(b"\r\n".join(out) + rest)
                self._tunnel(self.request, up)
        except Exception:
            try:
                self.request.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
            except Exception:
                pass

    def _tunnel(self, a: socket.socket, b: socket.socket) -> None:
        sockets = [a, b]
        try:
            while True:
                r, _, x = select.select(sockets, [], sockets, 60)
                if x or not r:
                    break
                for s in r:
                    other = b if s is a else a
                    try:
                        data = s.recv(65536)
                    except Exception:
                        return
                    if not data:
                        return
                    try:
                        other.sendall(data)
                    except Exception:
                        return
        finally:
            for s in (a, b):
                try:
                    s.close()
                except Exception:
                    pass


class _ThreadingTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def ensure_local_auth_proxy(upstream_proxy: str) -> str:
    """Return local proxy URL http://127.0.0.1:port for the upstream.

    If upstream has no userinfo, return upstream unchanged (no bridge needed).
    """
    p = (upstream_proxy or "").strip()
    if not p:
        return ""
    host, port, auth = _parse_upstream(p)
    if not auth:
        # No credentials — Chromium can use host:port directly
        scheme = urlparse(p if "://" in p else f"http://{p}").scheme or "http"
        return f"{scheme}://{host}:{port}"

    with _lock:
        existing = _bridges.get(p)
        if existing and existing.get("port"):
            return f"http://127.0.0.1:{existing['port']}"

        class H(_Handler):
            upstream_host = host
            upstream_port = port
            proxy_auth = auth

        server = _ThreadingTCPServer(("127.0.0.1", 0), H)
        local_port = int(server.server_address[1])
        t = threading.Thread(target=server.serve_forever, name=f"auth-proxy-{local_port}", daemon=True)
        t.start()
        _bridges[p] = {"port": local_port, "server": server, "thread": t, "upstream": p}
        return f"http://127.0.0.1:{local_port}"


def chromium_proxy_server(upstream_proxy: str | None) -> str:
    """Value suitable for Chromium --proxy-server / set_proxy."""
    p = (upstream_proxy or "").strip()
    if not p:
        return ""
    local = ensure_local_auth_proxy(p)
    # Chromium wants host:port or scheme://host:port without userinfo
    u = urlparse(local if "://" in local else f"http://{local}")
    h = u.hostname or "127.0.0.1"
    port = u.port or 80
    scheme = u.scheme or "http"
    return f"{scheme}://{h}:{port}"


def shutdown_all_local_proxies() -> None:
    with _lock:
        items = list(_bridges.values())
        _bridges.clear()
    for it in items:
        srv = it.get("server")
        if srv is not None:
            try:
                srv.shutdown()
            except Exception:
                pass
            try:
                srv.server_close()
            except Exception:
                pass
