from __future__ import annotations
from abc import ABC, abstractmethod
from pathlib import Path
from ..finding import ParsedFile


class Parser(ABC):
    @abstractmethod
    def parse_file(self, path: Path) -> ParsedFile:
        ...

    @abstractmethod
    def supports(self, path: Path) -> bool:
        ...
