from __future__ import annotations

import gzip
import hashlib
import shutil
from pathlib import Path


def write_sha256(path: Path) -> Path:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    out = Path(str(path) + ".sha256")
    out.write_text(f"{h.hexdigest()}  {path.name}\n", encoding="utf-8")
    return out


def maybe_gzip(path: Path, compress: str) -> Path:
    if (compress or "none").lower() not in {"gzip", "gz", "true", "1", "yes"}:
        write_sha256(path)
        return path
    gz_path = path.with_suffix(path.suffix + ".gz")
    with path.open("rb") as src, gzip.open(gz_path, "wb") as dst:
        shutil.copyfileobj(src, dst)
    path.unlink(missing_ok=True)
    write_sha256(gz_path)
    return gz_path


def verify_sha256(path: Path) -> bool:
    side = Path(str(path) + ".sha256")
    if not side.is_file():
        return True
    expected = side.read_text(encoding="utf-8").split()[0].strip()
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest() == expected
