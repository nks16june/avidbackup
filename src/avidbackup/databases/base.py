from __future__ import annotations

import dataclasses
import functools
from abc import ABC, abstractmethod
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class DumpResult:
    paths: list[Path] = field(default_factory=list)
    bytes_written: int = 0
    failed: int = 0
    source: str = ""
    table_report: Any | None = None


class DbDumper(ABC):
    name: str = "base"
    type: str = "base"

    def __init__(self, db_config: Any, settings: Any, *, remote_client: Any | None = None) -> None:
        self.db = db_config
        self.settings = settings
        self.remote = remote_client

    @abstractmethod
    def test_connection(self) -> None:
        ...

    @abstractmethod
    def dump(self, dest_dir: Path) -> DumpResult:
        ...

    def table_sizes(self):  # noqa: ANN201
        return None

    @contextmanager
    def _tunnel(self) -> Iterator[None]:
        """For via=ssh, point the connection at a local SSH-forwarded port while active."""
        if getattr(self.db, "via", "direct") != "ssh":
            yield
            return
        from avidbackup.ssh_tunnel import SshTunnel, ssh_settings_for

        remote_host = (self.db.host or "127.0.0.1").strip() or "127.0.0.1"
        with SshTunnel(
            ssh_settings_for(self.db, self.settings), remote_host, int(self.db.port or 0)
        ) as tunnel:
            original = self.db
            self.db = dataclasses.replace(original, host="127.0.0.1", port=tunnel.local_port)
            try:
                yield
            finally:
                self.db = original


def with_tunnel(method):  # noqa: ANN001, ANN201
    """Run a dumper method inside the SSH tunnel when the database is set to via=ssh."""

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
        with self._tunnel():
            return method(self, *args, **kwargs)

    return wrapper
