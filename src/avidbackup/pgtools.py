"""Optional, on-demand PostgreSQL client tools (pg_dump, psql, pg_restore) for Windows.

Only users who back up PostgreSQL need these. Instead of shipping them in the
installer, AvidBackup reads the official PostgreSQL Windows binaries archive
over HTTP range requests and extracts just the handful of files it needs
(about 5 MB downloaded from a 385 MB archive). Every file is checked against a
SHA256 pin in `data/pgtools_manifest.json` before it is installed.

Linux/macOS users should install the client tools with their package manager.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from avidbackup.paths import app_root

log = logging.getLogger(__name__)

MANIFEST_PATH = Path(__file__).resolve().parent / "data" / "pgtools_manifest.json"
BLOCK = 1024 * 1024
USER_AGENT = "AvidBackup-pgtools"
ProgressFn = Callable[[str], None]


class PgToolsError(RuntimeError):
    pass


def load_manifest() -> dict[str, Any]:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def tools_bin_dir(version: str | None = None, root: Path | None = None) -> Path:
    version = version or load_manifest()["version"]
    return (root or app_root()) / "tools" / "postgresql" / version / "bin"


def installed_tool(name: str, root: Path | None = None) -> str | None:
    """Path to a tool installed by `install_postgres_tools`, if present."""
    exe = name if name.lower().endswith(".exe") else f"{name}.exe"
    candidate = tools_bin_dir(root=root) / exe
    return str(candidate) if candidate.is_file() else None


class HttpRangeFile(io.RawIOBase):
    """Read-only, seekable file over HTTP using bounded Range requests."""

    def __init__(self, url: str, size: int, *, timeout: float = 60.0, retries: int = 3) -> None:
        self.url = url
        self.size = size
        self.timeout = timeout
        self.retries = retries
        self._pos = 0
        self._cache: dict[int, bytes] = {}
        self.requests = 0
        self.bytes_fetched = 0

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        if whence == io.SEEK_SET:
            self._pos = offset
        elif whence == io.SEEK_CUR:
            self._pos += offset
        elif whence == io.SEEK_END:
            self._pos = self.size + offset
        else:
            raise ValueError(f"invalid whence {whence}")
        self._pos = max(0, self._pos)
        return self._pos

    def _block(self, index: int) -> bytes:
        cached = self._cache.get(index)
        if cached is not None:
            return cached
        start = index * BLOCK
        end = min(start + BLOCK, self.size) - 1
        data = self._fetch(start, end)
        if len(self._cache) > 8:
            self._cache.pop(next(iter(self._cache)))
        self._cache[index] = data
        return data

    def _fetch(self, start: int, end: int) -> bytes:
        want = end - start + 1
        last: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                req = urllib.request.Request(
                    self.url,
                    headers={"Range": f"bytes={start}-{end}", "User-Agent": USER_AGENT},
                )
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310
                    if resp.status != 206:
                        raise PgToolsError(
                            f"Server did not honour range request (HTTP {resp.status})"
                        )
                    data = resp.read()
                if len(data) != want:
                    raise PgToolsError(f"Short read: wanted {want} bytes, got {len(data)}")
                self.requests += 1
                self.bytes_fetched += len(data)
                return data
            except (urllib.error.URLError, TimeoutError, ConnectionError, PgToolsError) as exc:
                last = exc
                log.warning("Range fetch %s-%s failed (attempt %s): %s", start, end, attempt, exc)
                time.sleep(min(2 * attempt, 6))
        raise PgToolsError(f"Download failed: {last}")

    def readinto(self, buf: Any) -> int:
        if self._pos >= self.size:
            return 0
        out = memoryview(buf)
        n = 0
        while n < len(out) and self._pos < self.size:
            index, offset = divmod(self._pos, BLOCK)
            block = self._block(index)
            chunk = block[offset : offset + (len(out) - n)]
            if not chunk:
                break
            out[n : n + len(chunk)] = chunk
            n += len(chunk)
            self._pos += len(chunk)
        return n


def _extract_verified(
    zf: zipfile.ZipFile, entry: dict[str, Any], dest_dir: Path, say: ProgressFn
) -> None:
    name = entry["name"]
    if Path(name).name != name:
        raise PgToolsError(f"Unsafe file name in manifest: {name!r}")
    try:
        info = zf.getinfo(entry["zip_path"])
    except KeyError as exc:
        raise PgToolsError(f"{entry['zip_path']} not found in archive") from exc
    if info.file_size != entry["size"]:
        raise PgToolsError(
            f"{name}: size {info.file_size} does not match pinned {entry['size']}"
        )
    say(f"Downloading {name} ({info.file_size // 1024} KB)")
    tmp = dest_dir / f".{name}.part"
    digest = hashlib.sha256()
    with zf.open(info) as src, tmp.open("wb") as out:
        while True:
            chunk = src.read(256 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            out.write(chunk)
    if digest.hexdigest() != entry["sha256"]:
        tmp.unlink(missing_ok=True)
        raise PgToolsError(f"{name}: SHA256 mismatch, refusing to install")
    os.replace(tmp, dest_dir / name)


def install_postgres_tools(
    progress: ProgressFn | None = None,
    *,
    root: Path | None = None,
    url: str | None = None,
    manifest: dict[str, Any] | None = None,
) -> Path:
    """Download and install pg_dump/psql/pg_restore. Returns the bin directory."""
    say = progress or (lambda msg: log.info(msg))
    if sys.platform != "win32":
        raise PgToolsError(
            "Automatic install is Windows-only. Use your package manager, e.g. "
            "'sudo apt install postgresql-client' or 'brew install libpq'."
        )
    man = manifest or load_manifest()
    dest = tools_bin_dir(man["version"], root)
    dest.mkdir(parents=True, exist_ok=True)

    src_url = url or man["url"]
    rf = HttpRangeFile(src_url, int(man["zip_size"]))
    say(f"Reading PostgreSQL {man['version']} file list...")
    try:
        zf = zipfile.ZipFile(rf)
    except (zipfile.BadZipFile, PgToolsError) as exc:
        raise PgToolsError(f"Could not read the PostgreSQL archive: {exc}") from exc
    with zf:
        for entry in [*man["files"], *man.get("licenses", [])]:
            _extract_verified(zf, entry, dest, say)
    (dest.parent / "installed.json").write_text(
        json.dumps({"version": man["version"], "source": man["source_page"]}, indent=2),
        encoding="utf-8",
    )
    say(
        f"PostgreSQL tools {man['version']} installed to {dest} "
        f"({rf.bytes_fetched // 1024} KB downloaded)"
    )
    return dest
