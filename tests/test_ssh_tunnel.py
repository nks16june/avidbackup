from __future__ import annotations

import socket
from types import SimpleNamespace

import paramiko
import pytest
from ssh_test_server import PASSWORD, USER, EchoServer, SshTestServer

from avidbackup.databases.base import DbDumper, DumpResult
from avidbackup.paths import known_hosts_path
from avidbackup.ssh_tunnel import SshSettings, SshTunnel, TunnelError, ssh_settings_for
from avidbackup.store import DatabaseConfig


@pytest.fixture(scope="module")
def host_key():
    return paramiko.RSAKey.generate(2048)


@pytest.fixture
def echo():
    server = EchoServer()
    yield server
    server.close()


@pytest.fixture
def ssh_server(host_key):
    server = SshTestServer(host_key)
    yield server
    server.close()


def _settings(server, **over) -> SshSettings:
    base = {"host": "127.0.0.1", "port": server.port, "user": USER, "password": PASSWORD,
            "host_key_policy": "insecure"}
    base.update(over)
    return SshSettings(**base)


def _roundtrip(port: int, payload: bytes = b"hello through the tunnel") -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=10) as conn:
        conn.sendall(payload)
        conn.settimeout(10)
        return conn.recv(65536)


def test_forwards_traffic_to_the_remote_port(ssh_server, echo):
    with SshTunnel(_settings(ssh_server), "127.0.0.1", echo.port) as tunnel:
        assert tunnel.local_port > 0
        assert _roundtrip(tunnel.local_port) == b"hello through the tunnel"
        big = b"x" * 300_000
        with socket.create_connection(("127.0.0.1", tunnel.local_port), timeout=10) as conn:
            conn.sendall(big)
            got = b""
            conn.settimeout(10)
            while len(got) < len(big):
                chunk = conn.recv(65536)
                if not chunk:
                    break
                got += chunk
        assert got == big
    assert f"127.0.0.1:{echo.port}" in ssh_server.logins


def test_tunnel_port_is_closed_after_use(ssh_server, echo):
    with SshTunnel(_settings(ssh_server), "127.0.0.1", echo.port) as tunnel:
        port = tunnel.local_port
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=2)


def test_wrong_password_is_reported(ssh_server, echo):
    with pytest.raises(TunnelError, match="Could not open the SSH connection"):
        with SshTunnel(_settings(ssh_server, password="nope"), "127.0.0.1", echo.port):
            pass


def test_missing_credentials_are_reported(ssh_server, echo):
    with pytest.raises(TunnelError, match="No SSH password or key file"):
        with SshTunnel(_settings(ssh_server, password=""), "127.0.0.1", echo.port):
            pass


def test_host_key_is_trusted_on_first_use_then_checked(backup_home, host_key, echo):
    first = SshTestServer(host_key)
    try:
        with SshTunnel(_settings(first, host_key_policy="tofu"), "127.0.0.1", echo.port) as t:
            assert _roundtrip(t.local_port)
        assert known_hosts_path().is_file()
        # same key again: accepted
        with SshTunnel(_settings(first, host_key_policy="strict"), "127.0.0.1", echo.port) as t:
            assert _roundtrip(t.local_port)
    finally:
        first.close()

    # a different server key on the same host:port must be refused
    impostor = SshTestServer(paramiko.RSAKey.generate(2048))
    try:
        entry = known_hosts_path().read_text(encoding="utf-8")
        known_hosts_path().write_text(entry.replace(f"[127.0.0.1]:{first.port}", f"[127.0.0.1]:{impostor.port}"),
                                      encoding="utf-8")
        with pytest.raises(TunnelError, match="HOST KEY MISMATCH"):
            with SshTunnel(_settings(impostor, host_key_policy="tofu"), "127.0.0.1", echo.port):
                pass
    finally:
        impostor.close()


def test_strict_policy_refuses_unknown_hosts(backup_home, ssh_server, echo):
    with pytest.raises(TunnelError, match="strict"):
        with SshTunnel(_settings(ssh_server, host_key_policy="strict"), "127.0.0.1", echo.port):
            pass


def test_unreachable_ssh_server_is_reported():
    dead = socket.socket()
    dead.bind(("127.0.0.1", 0))
    port = dead.getsockname()[1]
    dead.close()
    with pytest.raises(TunnelError, match="Could not open the SSH connection"):
        with SshTunnel(SshSettings(host="127.0.0.1", port=port, user="u", password="p"), "127.0.0.1", 3306,
                       connect_timeout=3):
            pass


# ---- settings resolution ------------------------------------------------------------------


def test_sftp_login_can_be_reused():
    site = SimpleNamespace(protocol="sftp", host="h.example", port=2222, user="u", password="p")
    db = DatabaseConfig(id="d", via="ssh", ssh_same_as_ftp=True)
    got = ssh_settings_for(db, site)
    assert (got.host, got.port, got.user, got.password) == ("h.example", 2222, "u", "p")


def test_sftp_login_cannot_be_reused_for_plain_ftp():
    site = SimpleNamespace(protocol="ftp", host="h", port=21, user="u", password="p")
    with pytest.raises(TunnelError, match="SFTP"):
        ssh_settings_for(DatabaseConfig(id="d", via="ssh", ssh_same_as_ftp=True), site)


def test_own_ssh_login_and_secret_from_environment(monkeypatch):
    monkeypatch.setenv("MY_SSH_PW", "from-env")
    db = DatabaseConfig(id="d", via="ssh", ssh_host="ssh.example", ssh_port=2200, ssh_user="me",
                        ssh_password_env="MY_SSH_PW")
    got = ssh_settings_for(db, SimpleNamespace(protocol="ftp"))
    assert (got.host, got.port, got.user, got.password) == ("ssh.example", 2200, "me", "from-env")


# ---- integration with the dumper base class -----------------------------------------------


class _ProbeDumper(DbDumper):
    name = type = "probe"

    def test_connection(self) -> None:
        return None

    def dump(self, dest_dir):  # noqa: ANN001
        return DumpResult()


def test_dumper_connects_through_the_tunnel_then_restores_its_config(ssh_server, echo):
    db = DatabaseConfig(id="d", via="ssh", host="127.0.0.1", port=echo.port, ssh_host="127.0.0.1",
                        ssh_port=ssh_server.port, ssh_user=USER, ssh_password=PASSWORD,
                        ssh_host_key_policy="insecure")
    dumper = _ProbeDumper(db, SimpleNamespace(protocol="ftp"))
    with dumper._tunnel():
        assert dumper.db.host == "127.0.0.1"
        assert dumper.db.port not in (0, echo.port)  # a local forwarded port
        assert _roundtrip(dumper.db.port) == b"hello through the tunnel"
    assert dumper.db is db and dumper.db.port == echo.port  # original config is back


def test_direct_databases_do_not_open_a_tunnel():
    db = DatabaseConfig(id="d", via="direct", host="db.example", port=3306)
    dumper = _ProbeDumper(db, SimpleNamespace())
    with dumper._tunnel():
        assert dumper.db is db
