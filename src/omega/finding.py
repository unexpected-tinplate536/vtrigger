from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
import hashlib
import json


@dataclass
class Import:
    name: str           # what's imported
    source: str | None  # where it's from (module path)
    line: int
    alias: str | None = None


@dataclass
class Export:
    name: str
    line: int
    kind: str  # "function", "class", "variable", "default"


@dataclass
class Function:
    name: str
    line: int
    end_line: int
    param_count: int
    line_count: int


@dataclass
class Class:
    name: str
    line: int
    end_line: int
    method_count: int
    line_count: int


@dataclass
class Variable:
    name: str
    line: int
    scope: str  # "module", "function", "class"


@dataclass
class Call:
    name: str
    line: int


@dataclass
class ParsedFile:
    path: Path
    imports: list[Import] = field(default_factory=list)
    exports: list[Export] = field(default_factory=list)
    functions: list[Function] = field(default_factory=list)
    classes: list[Class] = field(default_factory=list)
    variables: list[Variable] = field(default_factory=list)
    calls: list[Call] = field(default_factory=list)
    raw_lines: int = 0

    def to_cache_dict(self) -> dict:
        """Serialize ParsedFile to a JSON-compatible dict for caching."""
        return {
            "path": str(self.path),
            "imports": [
                {"name": i.name, "source": i.source, "line": i.line, "alias": i.alias}
                for i in self.imports
            ],
            "exports": [
                {"name": e.name, "line": e.line, "kind": e.kind}
                for e in self.exports
            ],
            "functions": [
                {"name": f.name, "line": f.line, "end_line": f.end_line,
                 "param_count": f.param_count, "line_count": f.line_count}
                for f in self.functions
            ],
            "classes": [
                {"name": c.name, "line": c.line, "end_line": c.end_line,
                 "method_count": c.method_count, "line_count": c.line_count}
                for c in self.classes
            ],
            "variables": [
                {"name": v.name, "line": v.line, "scope": v.scope}
                for v in self.variables
            ],
            "calls": [
                {"name": c.name, "line": c.line}
                for c in self.calls
            ],
            "raw_lines": self.raw_lines,
        }

    @classmethod
    def from_cache_dict(cls, d: dict) -> ParsedFile:
        """Reconstruct a ParsedFile from a cached dict."""
        return cls(
            path=Path(d["path"]),
            imports=[Import(**i) for i in d.get("imports", [])],
            exports=[Export(**e) for e in d.get("exports", [])],
            functions=[Function(**f) for f in d.get("functions", [])],
            classes=[Class(**c) for c in d.get("classes", [])],
            variables=[Variable(**v) for v in d.get("variables", [])],
            calls=[Call(**c) for c in d.get("calls", [])],
            raw_lines=d.get("raw_lines", 0),
        )


@dataclass
class Finding:
    detector: str       # "dead_code", "unused_imports", etc.
    category: str       # "unused_function", "unused_import", etc.
    message: str        # human-readable
    file: str           # relative path
    line: int | None = None
    snippet: str | None = None
    related: list[str] = field(default_factory=list)
    confidence: str = "high"  # "high" or "medium"

    @property
    def hash(self) -> str:
        """Stable hash for deduplication across rescans."""
        key = f"{self.detector}:{self.category}:{self.file}:{self.line}"
        return hashlib.sha256(key.encode()).hexdigest()[:12]

    def to_dict(self) -> dict:
        return {
            "hash": self.hash,
            "detector": self.detector,
            "category": self.category,
            "message": self.message,
            "file": self.file,
            "line": self.line,
            "snippet": self.snippet,
            "related": self.related,
            "confidence": self.confidence,
        }

    @classmethod
    def from_dict(cls, d: dict) -> Finding:
        d = {k: v for k, v in d.items() if k != "hash"}
        return cls(**d)
