from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class FileState:
    size: int
    mtime: float | None
    local_path: str


class Manifest:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._files: dict[str, FileState] = {}

    def load(self) -> None:
        if not self.path.is_file():
            self._files = {}
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("Corrupt manifest %s (%s); starting fresh", self.path, exc)
            self._files = {}
            return
        files = raw.get("files", {}) if isinstance(raw, dict) else {}
        self._files = {
            key: FileState(
                size=int(val.get("size", 0)),
                mtime=val.get("mtime"),
                local_path=val.get("local_path", ""),
            )
            for key, val in files.items()
            if isinstance(val, dict)
        }

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "files": {k: asdict(v) for k, v in sorted(self._files.items())},
        }
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    @property
    def entries(self) -> dict[str, FileState]:
        return self._files

    def get(self, remote_path: str) -> FileState | None:
        return self._files.get(remote_path)

    def set(self, remote_path: str, state: FileState) -> None:
        self._files[remote_path] = state

    def needs_download(
        self,
        remote_path: str,
        size: int,
        mtime: float | None,
        local_path: Path,
        incremental: bool,
    ) -> bool:
        """
        Return True if the remote file must be downloaded.

        Incremental mode compares **size + mtime** against the local copy (and
        the manifest). A changed local path alone (day-folder / hierarchy remap)
        does **not** force a re-download when the file already exists at the new
        location with matching size/mtime — we only refresh the manifest path.
        """
        if not incremental:
            return True
        if not local_path.is_file():
            return True
        try:
            st = local_path.stat()
        except OSError:
            return True
        if st.st_size != size:
            return True

        prev = self.get(remote_path)
        if prev is None:
            # Hardlinked / copied into today's folder but not yet in manifest.
            if mtime is not None and abs(st.st_mtime - float(mtime)) > 2.0:
                return True
            return False

        if prev.size != size:
            return True
        if mtime is not None and prev.mtime is not None and abs(float(mtime) - float(prev.mtime)) > 1.0:
            return True
        # Remote mtime unknown: size match is enough when local already present.
        if mtime is not None and prev.mtime is None and abs(st.st_mtime - float(mtime)) > 2.0:
            return True
        return False

