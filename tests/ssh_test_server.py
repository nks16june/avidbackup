"""A tiny in-process SSH server (paramiko) that supports password auth and direct-tcpip
forwarding, used to test the SSH tunnel without any external services."""

from __future__ import annotations

import socket
import threading

import paramiko

USER = "tester"
PASSWORD = "pw-123-test"


class _Interface(paramiko.ServerInterface):
    def __init__(self) -> None:
        self.destinations: dict[int, tuple[str, int]] = {}

    def get_allowed_auths(self, username: str) -> str:
        return "password"

    def check_auth_password(self, username: str, password: str) -> int:
        ok = username == USER and password == PASSWORD
        return paramiko.AUTH_SUCCESSFUL if ok else paramiko.AUTH_FAILED

    def check_channel_request(self, kind: str, chanid: int) -> int:
        return paramiko.OPEN_SUCCEEDED if kind == "session" else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_direct_tcpip_request(self, chanid, origin, destination) -> int:  # noqa: ANN001
        self.destinations[chanid] = destination
        return paramiko.OPEN_SUCCEEDED


def _relay(chan: paramiko.Channel, dest: tuple[str, int]) -> None:
    try:
        upstream = socket.create_connection(dest, timeout=10)
    except OSError:
        chan.close()
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

    threading.Thread(target=pump, args=(chan.recv, upstream.sendall), daemon=True).start()
    threading.Thread(target=pump, args=(upstream.recv, chan.sendall), daemon=True).start()
    done.wait()
    for closer in (chan.close, upstream.close):
        try:
            closer()
        except Exception:  # noqa: BLE001
            pass


class SshTestServer:
    def __init__(self, host_key: paramiko.PKey) -> None:
        self.host_key = host_key
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.sock.settimeout(0.5)
        self.port = self.sock.getsockname()[1]
        self._stop = threading.Event()
        self.logins: list[str] = []
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                client, _addr = self.sock.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            threading.Thread(target=self._handle, args=(client,), daemon=True).start()

    def _handle(self, client: socket.socket) -> None:
        transport = paramiko.Transport(client)
        transport.add_server_key(self.host_key)
        iface = _Interface()
        try:
            transport.start_server(server=iface)
        except Exception:  # noqa: BLE001
            return
        while transport.is_active() and not self._stop.is_set():
            chan = transport.accept(1)
            if chan is None:
                continue
            dest = iface.destinations.get(chan.chanid)
            if dest:
                self.logins.append(f"{dest[0]}:{dest[1]}")
                threading.Thread(target=_relay, args=(chan, dest), daemon=True).start()

    def close(self) -> None:
        self._stop.set()
        try:
            self.sock.close()
        except OSError:
            pass


class EchoServer:
    """Echoes back whatever it receives (stands in for a database port)."""

    def __init__(self) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.sock.settimeout(0.5)
        self.port = self.sock.getsockname()[1]
        self._stop = threading.Event()
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            threading.Thread(target=self._echo, args=(conn,), daemon=True).start()

    @staticmethod
    def _echo(conn: socket.socket) -> None:
        try:
            while True:
                data = conn.recv(65536)
                if not data:
                    break
                conn.sendall(data)
        except OSError:
            pass
        finally:
            conn.close()

    def close(self) -> None:
        self._stop.set()
        try:
            self.sock.close()
        except OSError:
            pass
