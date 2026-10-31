from __future__ import annotations

from avidbackup.netutil import find_free_port


def test_find_free_port_preferred_or_ephemeral():
    port = find_free_port(8787, host="127.0.0.1")
    assert 1 <= port <= 65535
