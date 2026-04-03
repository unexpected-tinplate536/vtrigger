from __future__ import annotations

import fnmatch
import os
from pathlib import Path

from .config import Config
from .finding import Finding, ParsedFile
from .state import State
from .parsers.python import PythonParser
from .parsers.javascript import JavaScriptParser
from .parsers.solidity import SolidityParser
from .parsers.go import GoParser
from .parsers.rust import RustParser
from .parsers.java import JavaParser
from .parsers.cpp import CppParser
from .parsers.ruby import RubyParser
from .parsers.php import PhpParser
from .parsers.swift import SwiftParser
from .parsers.kotlin import KotlinParser
from .parsers.dart import DartParser
from .parsers.lua import LuaParser
from .parsers.elixir import ElixirParser
from .parsers.zig import ZigParser
from .parsers.shell import ShellParser
from .parsers.nim import NimParser
from .parsers.scala import ScalaParser
from .parsers.objc import ObjCParser
from .parsers.erlang import ErlangParser
from .detectors.unused_imports import UnusedImportsDetector
from .detectors.dead_code import DeadCodeDetector
from .detectors.size import SizeDetector
from .detectors.secrets import SecretsDetector
from .detectors.cycles import CycleDetector
from .detectors.duplication import DuplicationDetector


SUPPORTED_EXTENSIONS = {
    ".py", ".js", ".jsx", ".ts", ".tsx",  # Python, JS/TS
    ".sol",                                 # Solidity
    ".go",                                  # Go
    ".rs",                                  # Rust
    ".java",                                # Java
    ".c", ".cpp", ".cc", ".cxx",           # C/C++ source
    ".h", ".hpp", ".hxx",                   # C/C++ headers
    ".rb",                                  # Ruby
    ".php",                                 # PHP
    ".swift",                               # Swift
    ".kt", ".kts",                          # Kotlin
    ".dart",                                # Dart
    ".lua",                                 # Lua
    ".ex", ".exs",                          # Elixir
    ".zig",                                 # Zig
    ".sh", ".bash", ".zsh",                 # Shell
    ".nim", ".nims",                        # Nim
    ".scala", ".sc",                        # Scala
    ".m", ".mm",                            # Objective-C
    ".erl", ".hrl",                         # Erlang
}
# Skip generated/declaration files
SKIP_SUFFIXES = (".d.ts", ".d.tsx", ".min.js", ".min.css")


class Scanner:
    def __init__(self, path: Path, config: Config):
        self.path = path.resolve()
        self.config = config
        self.parsers = [
            PythonParser(), JavaScriptParser(), SolidityParser(),
            GoParser(), RustParser(), JavaParser(), CppParser(),
            RubyParser(), PhpParser(), SwiftParser(), KotlinParser(),
            DartParser(), LuaParser(), ElixirParser(), ZigParser(),
            ShellParser(), NimParser(),
            ScalaParser(), ObjCParser(), ErlangParser(),
        ]
        self.detectors = [
            UnusedImportsDetector(),
            DeadCodeDetector(),
            SizeDetector(config),
            SecretsDetector(),
            CycleDetector(),
            DuplicationDetector(config),
        ]

    def discover_files(self) -> list[Path]:
        """Walk the project tree, skip ignored dirs early, return supported files."""
        supported_extensions = SUPPORTED_EXTENSIONS
        # Pre-compute directory names to skip (extract from glob patterns like "node_modules/**")
        skip_dirs = set()
        for pattern in self.config.ignore:
            # "node_modules/**" or "node_modules" -> skip "node_modules"
            base = pattern.rstrip("/*")
            if "/" not in base and base == base.strip("*"):
                skip_dirs.add(base)

        files: list[Path] = []
        for dirpath, dirnames, filenames in os.walk(self.path):
            # Prune ignored directories in-place (prevents os.walk from descending)
            dirnames[:] = [
                d for d in dirnames
                if d not in skip_dirs and not d.startswith(".")
            ]

            for filename in filenames:
                filepath = Path(dirpath) / filename
                if filepath.suffix not in supported_extensions:
                    continue
                if any(str(filepath).endswith(s) for s in SKIP_SUFFIXES):
                    continue

                rel = str(filepath.relative_to(self.path))
                if self._is_ignored(rel):
                    continue

                files.append(filepath)

        return sorted(files)

    def _is_ignored(self, rel_path: str) -> bool:
        """Check if a relative path matches any ignore pattern."""
        for pattern in self.config.ignore:
            if fnmatch.fnmatch(rel_path, pattern):
                return True
            # Check each path segment against directory patterns
            parts = rel_path.split("/")
            for i in range(len(parts)):
                partial = "/".join(parts[: i + 1])
                if fnmatch.fnmatch(partial, pattern):
                    return True
        return False

    def parse_files(self, files: list[Path]) -> list[ParsedFile]:
        """Route each file to its matching parser and return parsed results."""
        parsed: list[ParsedFile] = []
        for filepath in files:
            for parser in self.parsers:
                if parser.supports(filepath):
                    parsed.append(parser.parse_file(filepath))
                    break
        return parsed

    def parse_files_incremental(self, files: list[Path], state: State) -> list[ParsedFile]:
        """Parse files incrementally, reusing cached results for unchanged files.

        For each file, checks if its mtime matches the cached mtime.
        If so, reconstructs ParsedFile from cache. Otherwise, parses fresh
        and updates the cache.
        """
        cache = state.load_parse_cache()
        parsed: list[ParsedFile] = []
        updated = False

        for filepath in files:
            rel_key = str(filepath.relative_to(self.path))
            try:
                current_mtime = filepath.stat().st_mtime
            except OSError:
                continue

            # Check cache hit
            cached_entry = cache.get(rel_key)
            if cached_entry is not None and cached_entry.get("mtime") == current_mtime:
                parsed.append(ParsedFile.from_cache_dict(cached_entry["data"]))
                continue

            # Cache miss: parse the file
            for parser in self.parsers:
                if parser.supports(filepath):
                    pf = parser.parse_file(filepath)
                    parsed.append(pf)
                    cache[rel_key] = {
                        "mtime": current_mtime,
                        "data": pf.to_cache_dict(),
                    }
                    updated = True
                    break

        # Remove stale entries (files no longer in the file list)
        current_keys = {str(f.relative_to(self.path)) for f in files}
        stale_keys = [k for k in cache if k not in current_keys]
        for k in stale_keys:
            del cache[k]
            updated = True

        if updated:
            state.save_parse_cache(cache)

        return parsed

    def detect(self, parsed: list[ParsedFile]) -> list[Finding]:
        """Run detectors on parsed files, relativize paths, filter allowlist."""
        enabled_detectors = [
            d for d in self.detectors
            if d.name not in self.config.disabled_detectors
        ]

        findings: list[Finding] = []
        for detector in enabled_detectors:
            findings.extend(detector.run(parsed))

        # Relativize file paths in findings
        for f in findings:
            try:
                f.file = str(Path(f.file).relative_to(self.path))
            except ValueError:
                pass
            f.related = [
                str(Path(r).relative_to(self.path)) if Path(r).is_absolute() else r
                for r in f.related
            ]

        # Filter out allowlisted findings
        if self.config.allowlist:
            findings = [
                f for f in findings
                if f.hash not in self.config.allowlist.get(f.detector, [])
            ]

        return findings

    def scan(self) -> list[Finding]:
        """Orchestrate: discover files, parse them, run detectors, return findings."""
        files = self.discover_files()
        parsed = self.parse_files(files)
        return self.detect(parsed)
