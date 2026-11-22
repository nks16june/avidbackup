"""SSH port forwarding, so a database that only listens on the server can be backed up.

Many shared hosts block remote MySQL but allow SSH. AvidBackup opens an SSH connection
(same host-key checking as SFTP) and forwards a local port to the database as seen from
the server. The database dumpers then connect to 127.0.0.1:<local port>.
"""

from __future__ import annotations

import logging
import socket
import threading
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from avidbackup.secure_cli import resolve_secret

log = logging.getLogger(__name__)


class TunnelError(RuntimeError):
    pass


@dataclass
class SshSettings:
    host: str
    port: int = 22
    user: str = ""
    password: str = ""
    key_file: str = ""
    key_passphrase: str = ""
    host_key_policy: str = "tofu"


def ssh_settings_for(db: Any, settings: Any) -> SshSettings:
    """Work out the SSH login for a database entry (its own, or the site's SFTP login)."""
    if getattr(db, "ssh_same_as_ftp", False) or not getattr(db, "ssh_host", ""):
        protocol = str(getattr(settings, "protocol", "") or "").lower()
        if protocol != "sftp":
            raise TunnelError(
                "SSH tunnel needs SSH login details. Fill in the SSH host and user, or set the "
                "site's file access to SFTP and tick 'use the site's SFTP login'."
            )
        return SshSettings(
            host=settings.host,
            port=int(settings.port or 22),
            user=settings.user,
            password=settings.password,
            host_key_policy=str(getattr(settings, "host_key_policy", None) or "tofu").lower(),
        )
    password = ""
    if getattr(db, "ssh_password", "") or getattr(db, "ssh_password_env", ""):
        password = resolve_secret(db.ssh_password, getattr(db, "ssh_password_env", ""))
    passphrase = ""
    if getattr(db, "ssh_key_passphrase_env", ""):
        passphrase = resolve_secret("", db.ssh_key_passphrase_env)
    return SshSettings(
        host=db.ssh_host,
        port=int(db.ssh_port or 22),
        user=db.ssh_user,
        password=password,
        key_file=getattr(db, "ssh_key_file", ""),
        key_passphrase=passphrase,
        host_key_policy=str(getattr(db, "ssh_host_key_policy", "tofu") or "tofu").lower(),
    )


class SshTunnel(AbstractContextManager):
    """Forward 127.0.0.1:<random port> to remote_host:remote_port through an SSH server."""

    def __init__(
        self,
        ssh: SshSettings,
        remote_host: str,
        remote_port: int,
        *,
        connect_timeout: float = 30.0,
    ) -> None:
        self.ssh = ssh
        self.remote_host = remote_host or "127.0.0.1"
        self.remote_port = int(remote_port)
        self.connect_timeout = connect_timeout
        self.local_port = 0
        self._transport: Any = None
        self._server: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._conns: list[socket.socket] = []

    # -- connection ---------------------------------------------------------------------

    def _authenticate(self, transport: Any) -> None:
        import paramiko

        s = self.ssh
        if s.key_file:
            path = Path(s.key_file).expanduser()
            if not path.is_file():
                raise TunnelError(f"SSH key file not found: {path}")
            try:
                key = paramiko.PKey.from_path(str(path), passphrase=s.key_passphrase or None)
            except Exception as exc:  # noqa: BLE001
                raise TunnelError(f"Could not read the SSH key {path}: {exc}") from exc
            transport.auth_publickey(s.user, key)
        elif s.password:
            transport.auth_password(s.user, s.password)
        else:
            raise TunnelError("No SSH password or key file was provided.")

    def __enter__(self) -> SshTunnel:
        try:
            import paramiko
        except ImportError as exc:  # pragma: no cover
            raise TunnelError("SSH tunnels need paramiko (pip install paramiko).") from exc
        from avidbackup.remote import _verify_sftp_host_key

        s = self.ssh
        try:
            sock = socket.create_connection((s.host, s.port), timeout=self.connect_timeout)
            transport = paramiko.Transport(sock)
            transport.start_client(timeout=self.connect_timeout)
            _verify_sftp_host_key(
                s.host, s.port, transport.get_remote_server_key(), policy=s.host_key_policy
            )
            self._authenticate(transport)
            transport.set_keepalive(30)
        except (OSError, paramiko.SSHException, RuntimeError) as exc:
            # RuntimeError carries the host-key refusal (mismatch / strict policy).
            raise TunnelError(
                f"Could not open the SSH connection to {s.host}:{s.port}: {exc}"
            ) from exc
        self._transport = transport

        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen(8)
        server.settimeout(0.5)
        self._server = server
        self.local_port = server.getsockname()[1]
        self._thread = threading.Thread(
            target=self._accept_loop, name="avidbackup-ssh-tunnel", daemon=True
        )
        self._thread.start()
        log.info(
            "SSH tunnel up: 127.0.0.1:%s -> %s:%s via %s@%s",
            self.local_port,
            self.remote_host,
            self.remote_port,
            s.user,
            s.host,
        )
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        for conn in list(self._conns):
            try:
                conn.close()
            except OSError:
                pass
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=3)
        if self._transport is not None:
            try:
                self._transport.close()
            except Exception:  # noqa: BLE001
                pass
            self._transport = None

    # -- forwarding ---------------------------------------------------------------------

    def _accept_loop(self) -> None:
        assert self._server is not None
        while not self._stop.is_set():
            try:
                client, addr = self._server.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            self._conns.append(client)
            threading.Thread(
                target=self._serve, args=(client, addr), name="avidbackup-ssh-forward", daemon=True
            ).start()

    def _serve(self, client: socket.socket, addr: Any) -> None:
        try:
            channel = self._transport.open_channel(
                "direct-tcpip", (self.remote_host, self.remote_port), addr
            )
        except Exception as exc:  # noqa: BLE001
            log.error(
                "SSH server refused the connection to %s:%s: %s",
                self.remote_host,
                self.remote_port,
                exc,
            )
            client.close()
            return
        done = threading.Event()

        def pump(read, write) -> None:  # noqa: ANN001
            try:
                while not done.is_set():
                    data = read(65536)
                    if not data:
                        break
                    write(data)
            except Exception:  # noqa: BLE001
                pass
            finally:
                done.set()

        up = threading.Thread(target=pump, args=(client.recv, channel.sendall), daemon=True)
        down = threading.Thread(target=pump, args=(channel.recv, client.sendall), daemon=True)
        up.start()
        down.start()
        done.wait()
        for closer in (channel.close, client.close):
            try:
                closer()
            except Exception:  # noqa: BLE001
                pass
