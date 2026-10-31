from __future__ import annotations

import logging
import socket
import ssl
import stat
from abc import abstractmethod
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timezone
from ftplib import FTP, FTP_TLS, error_perm
from pathlib import Path

from avidbackup.config import Settings
from avidbackup.paths import known_hosts_path

ProgressCb = Callable[[int], None]

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RemoteEntry:
    path: str  # absolute posix-style remote path
    name: str
    size: int
    mtime: float | None  # unix timestamp if known
    is_dir: bool


class RemoteClient(AbstractContextManager):
    @abstractmethod
    def list_dir(self, remote_dir: str) -> list[RemoteEntry]:
        ...

    @abstractmethod
    def download_file(
        self,
        remote_path: str,
        local_path: Path,
        progress: ProgressCb | None = None,
    ) -> None:
        ...

    @abstractmethod
    def upload_bytes(self, remote_path: str, data: bytes) -> None:
        ...

    @abstractmethod
    def read_bytes(self, remote_path: str, max_bytes: int = 2_000_000) -> bytes:
        ...

    @abstractmethod
    def delete_file(self, remote_path: str) -> None:
        ...

    @abstractmethod
    def exists(self, remote_path: str) -> bool:
        ...

    def walk_files(self, remote_root: str) -> Iterator[RemoteEntry]:
        stack = [remote_root.rstrip("/") or "/"]
        seen: set[str] = set()
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            try:
                entries = self.list_dir(current)
            except Exception as exc:  # noqa: BLE001
                log.warning("Cannot list %s: %s", current, exc)
                continue
            for entry in entries:
                if entry.is_dir:
                    stack.append(entry.path)
                else:
                    yield entry


def connect(settings: Settings) -> RemoteClient:
    protocol = settings.protocol
    if protocol == "sftp":
        return SftpClient(settings)
    if protocol in {"ftp", "ftps"}:
        return FtpClient(settings)
    raise ValueError(f"Unsupported FTP_PROTOCOL={protocol!r} (use ftp, ftps, or sftp)")


class FtpClient(RemoteClient):
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.ftp: FTP | None = None

    def __enter__(self) -> FtpClient:
        s = self.settings
        verify_tls = bool(getattr(s, "verify_tls", True))
        if s.protocol == "ftps":
            if verify_tls:
                ctx = ssl.create_default_context()
                ftp: FTP = FTP_TLS(context=ctx)
            else:
                log.warning(
                    "FTPS certificate verification DISABLED for %s (verify_tls=false). "
                    "Use only for trusted self-signed hosts.",
                    s.host,
                )
                ftp = FTP_TLS()
        else:
            ftp = FTP()

        ftp.connect(s.host, s.port, timeout=60)
        ftp.login(s.user, s.password)

        if isinstance(ftp, FTP_TLS) and s.tls_explicit:
            ftp.prot_p()

        if s.passive:
            ftp.set_pasv(True)

        try:
            ftp.sendcmd("OPTS UTF8 ON")
        except error_perm:
            pass

        self.ftp = ftp
        log.info("Connected via %s to %s:%s as %s", s.protocol.upper(), s.host, s.port, s.user)
        return self

    def __exit__(self, *exc: object) -> None:
        if self.ftp is not None:
            try:
                self.ftp.quit()
            except (EOFError, OSError, error_perm):
                try:
                    self.ftp.close()
                except OSError:
                    pass
            self.ftp = None

    def _ftp(self) -> FTP:
        if self.ftp is None:
            raise RuntimeError("FTP client is not connected")
        return self.ftp

    def exists(self, remote_path: str) -> bool:
        ftp = self._ftp()
        parent, _, name = remote_path.rstrip("/").rpartition("/")
        parent = parent or "/"
        try:
            names = ftp.nlst(parent)
        except error_perm:
            return False
        return any(n.rstrip("/").endswith(name) for n in names)

    def list_dir(self, remote_dir: str) -> list[RemoteEntry]:
        ftp = self._ftp()
        entries: list[RemoteEntry] = []
        lines: list[str] = []

        def collector(line: str) -> None:
            lines.append(line)

        try:
            ftp.dir(remote_dir, collector)
        except error_perm as exc:
            # Empty or inaccessible
            log.debug("dir failed for %s: %s", remote_dir, exc)
            return []

        for line in lines:
            parsed = _parse_unix_dir_line(line, remote_dir)
            if parsed is not None:
                entries.append(parsed)

        # Fallback: nlst if dir listing produced nothing but path exists
        if not entries:
            try:
                names = ftp.nlst(remote_dir)
            except error_perm:
                return []
            for raw in names:
                name = raw.rstrip("/").rsplit("/", 1)[-1]
                if name in {".", ".."}:
                    continue
                path = _join_remote(remote_dir, name)
                is_dir = self._is_dir(path)
                size = 0
                if not is_dir:
                    try:
                        size = ftp.size(path) or 0
                    except error_perm:
                        size = 0
                entries.append(
                    RemoteEntry(path=path, name=name, size=size, mtime=None, is_dir=is_dir)
                )
        return entries

    def _is_dir(self, path: str) -> bool:
        ftp = self._ftp()
        cwd = ftp.pwd()
        try:
            ftp.cwd(path)
            ftp.cwd(cwd)
            return True
        except error_perm:
            return False

    def download_file(
        self,
        remote_path: str,
        local_path: Path,
        progress: ProgressCb | None = None,
    ) -> None:
        ftp = self._ftp()
        local_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = local_path.with_suffix(local_path.suffix + ".part")
        transferred = 0

        def writer(chunk: bytes) -> None:
            nonlocal transferred
            fh.write(chunk)
            transferred += len(chunk)
            if progress is not None:
                progress(transferred)

        with tmp.open("wb") as fh:
            ftp.retrbinary(f"RETR {remote_path}", writer, blocksize=65536)
        tmp.replace(local_path)

    def upload_bytes(self, remote_path: str, data: bytes) -> None:
        from io import BytesIO

        ftp = self._ftp()
        ftp.storbinary(f"STOR {remote_path}", BytesIO(data))

    def read_bytes(self, remote_path: str, max_bytes: int = 2_000_000) -> bytes:
        from io import BytesIO

        ftp = self._ftp()
        buf = BytesIO()

        def writer(chunk: bytes) -> None:
            if buf.tell() + len(chunk) > max_bytes:
                raise ValueError(f"Remote file exceeds {max_bytes} bytes: {remote_path}")
            buf.write(chunk)

        ftp.retrbinary(f"RETR {remote_path}", writer)
        return buf.getvalue()

    def delete_file(self, remote_path: str) -> None:
        ftp = self._ftp()
        try:
            ftp.delete(remote_path)
        except error_perm as exc:
            log.warning("Could not delete remote %s: %s", remote_path, exc)


class SftpClient(RemoteClient):
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._transport = None
        self.sftp = None

    def __enter__(self) -> SftpClient:
        try:
            import paramiko
        except ImportError as exc:
            raise ImportError(
                "SFTP requires paramiko. Run: pip install -r requirements.txt"
            ) from exc

        s = self.settings
        policy = (getattr(s, "host_key_policy", None) or "tofu").lower()
        sock = socket.create_connection((s.host, s.port), timeout=60)
        transport = paramiko.Transport(sock)
        transport.start_client(timeout=60)
        remote_key = transport.get_remote_server_key()
        _verify_sftp_host_key(s.host, s.port, remote_key, policy=policy)
        transport.auth_password(s.user, s.password)
        self._transport = transport
        self.sftp = paramiko.SFTPClient.from_transport(transport)
        log.info("Connected via SFTP to %s:%s as %s", s.host, s.port, s.user)
        return self

    def __exit__(self, *exc: object) -> None:
        if self.sftp is not None:
            self.sftp.close()
            self.sftp = None
        if self._transport is not None:
            self._transport.close()
            self._transport = None

    def _sftp(self):
        if self.sftp is None:
            raise RuntimeError("SFTP client is not connected")
        return self.sftp

    def exists(self, remote_path: str) -> bool:
        try:
            self._sftp().stat(remote_path)
            return True
        except OSError:
            return False

    def list_dir(self, remote_dir: str) -> list[RemoteEntry]:
        sftp = self._sftp()
        entries: list[RemoteEntry] = []
        for attr in sftp.listdir_attr(remote_dir):
            name = attr.filename
            if name in {".", ".."}:
                continue
            path = _join_remote(remote_dir, name)
            is_dir = stat.S_ISDIR(attr.st_mode or 0)
            entries.append(
                RemoteEntry(
                    path=path,
                    name=name,
                    size=int(attr.st_size or 0),
                    mtime=float(attr.st_mtime) if attr.st_mtime else None,
                    is_dir=is_dir,
                )
            )
        return entries

    def download_file(
        self,
        remote_path: str,
        local_path: Path,
        progress: ProgressCb | None = None,
    ) -> None:
        local_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = local_path.with_suffix(local_path.suffix + ".part")

        def cb(transferred: int, _total: int) -> None:
            if progress is not None:
                progress(transferred)

        self._sftp().get(remote_path, str(tmp), callback=cb if progress else None)
        tmp.replace(local_path)

    def upload_bytes(self, remote_path: str, data: bytes) -> None:
        from io import BytesIO

        self._sftp().putfo(BytesIO(data), remote_path)

    def read_bytes(self, remote_path: str, max_bytes: int = 2_000_000) -> bytes:
        from io import BytesIO

        buf = BytesIO()
        with self._sftp().open(remote_path, "rb") as fh:
            while True:
                chunk = fh.read(65536)
                if not chunk:
                    break
                if buf.tell() + len(chunk) > max_bytes:
                    raise ValueError(f"Remote file exceeds {max_bytes} bytes: {remote_path}")
                buf.write(chunk)
        return buf.getvalue()

    def delete_file(self, remote_path: str) -> None:
        try:
            self._sftp().remove(remote_path)
        except OSError as exc:
            log.warning("Could not delete remote %s: %s", remote_path, exc)


def _verify_sftp_host_key(host: str, port: int, remote_key, *, policy: str) -> None:
    """Trust-on-first-use / strict / insecure host-key check."""
    import paramiko

    if policy == "insecure":
        log.warning(
            "SFTP host key verification DISABLED for %s (host_key_policy=insecure).",
            host,
        )
        return

    path = known_hosts_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    host_keys = paramiko.HostKeys()
    if path.is_file():
        try:
            host_keys.load(str(path))
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not load known_hosts %s: %s", path, exc)

    # Match OpenSSH-style entries: hostname and [hostname]:port
    lookup_names = [host]
    if port and int(port) != 22:
        lookup_names.append(f"[{host}]:{int(port)}")

    fingerprint = remote_key.get_base64()
    key_type = remote_key.get_name()

    for name in lookup_names:
        if name in host_keys:
            stored = host_keys[name].get(key_type)
            if stored is None:
                continue
            if stored.get_base64() == fingerprint:
                log.info("SFTP host key OK for %s (%s)", name, key_type)
                return
            raise RuntimeError(
                f"SFTP HOST KEY MISMATCH for {name} ({key_type}). "
                f"Expected fingerprint ending …{stored.get_base64()[-16:]}, "
                f"got …{fingerprint[-16:]}. "
                "If the server was legitimately reinstalled, remove the entry from "
                f"{path} and reconnect (tofu) or set host_key_policy=insecure (not recommended)."
            )

    if policy == "strict":
        raise RuntimeError(
            f"SFTP host key for {host} not found in {path} and host_key_policy=strict. "
            "Connect once with tofu to record the key, or add it manually."
        )

    # Trust on first use
    name = lookup_names[-1] if int(port) != 22 else host
    host_keys.add(name, key_type, remote_key)
    host_keys.save(str(path))
    try:
        path.chmod(0o600)
    except OSError:
        pass
    log.warning(
        "SFTP TOFU: recorded new host key for %s (%s, …%s) in %s",
        name,
        key_type,
        fingerprint[-16:],
        path,
    )


def _join_remote(parent: str, name: str) -> str:
    if parent in {"", "/"}:
        return f"/{name}"
    return f"{parent.rstrip('/')}/{name}"


def _parse_unix_dir_line(line: str, parent: str) -> RemoteEntry | None:
    """Parse a typical `LIST` line: permissions, links, owner, group, size, date, name."""
    parts = line.split(maxsplit=8)
    if len(parts) < 9:
        return None
    perms, _links, _owner, _group, size_s, month, day, time_or_year, name = parts
    if name in {".", ".."}:
        return None
    # Symlink target: "name -> target"
    if " -> " in name:
        name = name.split(" -> ", 1)[0]
    is_dir = perms.startswith("d")
    try:
        size = int(size_s)
    except ValueError:
        size = 0
    mtime = _guess_mtime(month, day, time_or_year)
    return RemoteEntry(
        path=_join_remote(parent, name),
        name=name,
        size=size,
        mtime=mtime,
        is_dir=is_dir,
    )


def _guess_mtime(month: str, day: str, time_or_year: str) -> float | None:
    """Best-effort mtime from FTP LIST date fields."""
    try:
        now = datetime.now(timezone.utc)
        if ":" in time_or_year:
            dt = datetime.strptime(
                f"{month} {day} {now.year} {time_or_year}", "%b %d %Y %H:%M"
            ).replace(tzinfo=timezone.utc)
            # Future date means previous year (FTP convention)
            if dt > now:
                dt = dt.replace(year=now.year - 1)
        else:
            dt = datetime.strptime(
                f"{month} {day} {time_or_year}", "%b %d %Y"
            ).replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None

