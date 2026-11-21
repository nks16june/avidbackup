from __future__ import annotations

import socket

from avidbackup.netutil import find_free_port
from avidbackup.remote import acceptable_tls_name


def test_find_free_port_preferred_or_ephemeral():
    port = find_free_port(8787, host="127.0.0.1")
    assert 1 <= port <= 65535


def test_tls_name_must_share_the_site_domain():
    assert (
        acceptable_tls_name("ftp.example.com", ["server3.example.com"])
        == "server3.example.com"
    )
    assert acceptable_tls_name("ftp.example.com", ["evil.example.net"]) is None


def test_port_in_use_by_another_listener_is_not_reported_free():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        taken = busy.getsockname()[1]
        assert find_free_port(taken, host="127.0.0.1") != taken
