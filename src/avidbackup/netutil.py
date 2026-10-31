from __future__ import annotations

import socket


def find_free_port(preferred: int = 8787, host: str = "127.0.0.1") -> int:
    """Return preferred port if free, otherwise an ephemeral free port on host."""
    preferred = int(preferred or 8787)
    if _can_bind(host, preferred):
        return preferred
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host if host not in {"::", "::1"} else "127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _can_bind(host: str, port: int) -> bool:
    family = socket.AF_INET6 if ":" in host and host != "localhost" else socket.AF_INET
    bind_host = host
    if host in {"localhost", ""}:
        bind_host = "127.0.0.1"
        family = socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((bind_host, port))
        return True
    except OSError:
        return False
