from __future__ import annotations
from abc import ABC, abstractmethod
from ..finding import Finding, ParsedFile


class Detector(ABC):
    name: str

    @abstractmethod
    def run(self, files: list[ParsedFile]) -> list[Finding]:
        ...
