from __future__ import annotations

from abc import ABC, abstractmethod
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
