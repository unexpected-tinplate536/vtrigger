from __future__ import annotations

from pathlib import Path

from .base import Detector
from ..finding import Finding, ParsedFile


class CycleDetector(Detector):
    name = "cycles"

    # Extensions to try when resolving import sources
    _extensions = (".ts", ".tsx", ".js", ".jsx", ".py")

    def run(self, files: list[ParsedFile]) -> list[Finding]:
        # Build lookup structures from parsed files
        path_set: set[str] = set()
        # Map from resolved key (path without ext, or with /index stripped) to actual path
        path_lookup: dict[str, str] = {}

        for pf in files:
            p = str(pf.path)
            path_set.add(p)
            # Store without extension
            for ext in self._extensions:
                if p.endswith(ext):
                    stem = p[: -len(ext)]
                    path_lookup[stem] = p
                    # Also allow /index resolution (so "foo" resolves to "foo/index.ts")
                    if stem.endswith("/index"):
                        dir_stem = stem[: -len("/index")]
                        path_lookup[dir_stem] = p
                    break
            else:
                # No recognized extension; store as-is
                path_lookup[p] = p

        # Build adjacency graph: file_path -> set of imported file paths
        graph: dict[str, set[str]] = {str(pf.path): set() for pf in files}

        for pf in files:
            src_path = str(pf.path)
            src_dir = str(Path(src_path).parent)

            for imp in pf.imports:
                if imp.source is None:
                    continue

                resolved = self._resolve_import(
                    imp.source, src_dir, path_set, path_lookup
                )
                if resolved is not None and resolved != src_path:
                    graph[src_path].add(resolved)

        # Find cycles via DFS
        cycles = self._find_cycles(graph)

        # Convert to findings
        findings: list[Finding] = []
        for cycle in cycles:
            cycle_display = [self._short(f) for f in cycle]
            findings.append(
                Finding(
                    detector="cycles",
                    category="import_cycle",
                    message=f"Circular import: {' -> '.join(cycle_display)} -> {cycle_display[0]}",
                    file=cycle[0],
                    line=None,
                    snippet=None,
                    related=list(cycle[1:]),
                    confidence="high",
                )
            )

        return findings

    def _resolve_import(
        self,
        source: str,
        src_dir: str,
        path_set: set[str],
        path_lookup: dict[str, str],
    ) -> str | None:
        """Resolve an import source string to an actual file path in the parsed set."""

        # Relative imports: starts with . or ..
        if source.startswith("."):
            # Resolve relative to the importing file's directory
            resolved = str(Path(src_dir, source).resolve())
            # Also try without resolve for non-absolute paths
            if not Path(src_dir).is_absolute():
                resolved = str(Path(src_dir) / source)
                resolved = _normalize_path(resolved)

            return self._match_path(resolved, path_set, path_lookup)

        # Non-relative imports: try to match against parsed files
        # Skip things that look like external packages (contain no path separators
        # and don't match any local file)
        return self._match_non_relative(source, path_set, path_lookup)

    def _match_path(
        self,
        candidate: str,
        path_set: set[str],
        path_lookup: dict[str, str],
    ) -> str | None:
        """Try to match a resolved path candidate to a file in the parsed set."""
        # Direct match
        if candidate in path_set:
            return candidate

        # Try in lookup (already has extension stripped)
        if candidate in path_lookup:
            return path_lookup[candidate]

        # Try adding extensions
        for ext in self._extensions:
            full = candidate + ext
            if full in path_set:
                return full

        # Try as directory import (candidate/index.ext)
        for ext in self._extensions:
            index = candidate + "/index" + ext
            if index in path_set:
                return index

        return None

    def _match_non_relative(
        self,
        source: str,
        path_set: set[str],
        path_lookup: dict[str, str],
    ) -> str | None:
        """Try to match a non-relative import against parsed files by suffix."""
        # Check if any parsed file path ends with the source (+ extension)
        for ext in self._extensions:
            suffix = "/" + source + ext
            for p in path_set:
                if p.endswith(suffix) or p == source + ext:
                    return p
            # Also try /index variant
            index_suffix = "/" + source + "/index" + ext
            for p in path_set:
                if p.endswith(index_suffix) or p == source + "/index" + ext:
                    return p

        return None

    def _find_cycles(self, graph: dict[str, set[str]]) -> list[list[str]]:
        """Find all minimal cycles in the graph using DFS."""
        visited: set[str] = set()
        visiting: set[str] = set()
        path: list[str] = []
        raw_cycles: list[list[str]] = []

        def dfs(node: str) -> None:
            if node in visited:
                return
            if node in visiting:
                # Found a cycle: extract it from the current path
                idx = path.index(node)
                cycle = path[idx:]
                raw_cycles.append(cycle)
                return

            visiting.add(node)
            path.append(node)

            for neighbor in sorted(graph.get(node, [])):
                dfs(neighbor)

            path.pop()
            visiting.discard(node)
            visited.add(node)

        for node in sorted(graph):
            dfs(node)

        # Deduplicate: normalize each cycle so the smallest path is first
        seen: set[tuple[str, ...]] = set()
        unique: list[list[str]] = []
        for cycle in raw_cycles:
            normalized = _normalize_cycle(cycle)
            key = tuple(normalized)
            if key not in seen:
                seen.add(key)
                unique.append(normalized)

        return unique

    @staticmethod
    def _short(path: str) -> str:
        """Return a shorter display name for a file path."""
        return path


def _normalize_path(p: str) -> str:
    """Normalize a path by resolving .. and . segments without hitting the filesystem."""
    parts: list[str] = []
    for segment in p.replace("\\", "/").split("/"):
        if segment == "." or segment == "":
            continue
        if segment == "..":
            if parts:
                parts.pop()
        else:
            parts.append(segment)
    return "/".join(parts)


def _normalize_cycle(cycle: list[str]) -> list[str]:
    """Normalize a cycle so it starts with the lexicographically smallest element."""
    if not cycle:
        return cycle
    min_idx = cycle.index(min(cycle))
    return cycle[min_idx:] + cycle[:min_idx]
