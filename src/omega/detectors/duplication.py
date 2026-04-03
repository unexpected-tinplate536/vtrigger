from __future__ import annotations

import hashlib
import re
from collections import defaultdict

from .base import Detector
from ..config import Config
from ..finding import Finding, ParsedFile, Function


class DuplicationDetector(Detector):
    name = "duplication"

    def __init__(self, config: Config, min_lines: int = 5) -> None:
        self.threshold = config.thresholds.duplication_min_copies
        self.min_lines = min_lines

    def run(self, files: list[ParsedFile]) -> list[Finding]:
        # Map structural hash -> list of (ParsedFile, Function) tuples
        hash_groups: dict[str, list[tuple[ParsedFile, Function]]] = defaultdict(list)

        for pf in files:
            if not pf.functions:
                continue

            try:
                source_lines = pf.path.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeDecodeError):
                continue

            for func in pf.functions:
                if func.line_count < self.min_lines:
                    continue

                body = self._extract_body(source_lines, func)
                if not body:
                    continue

                normalized = self._normalize(body)
                if not normalized:
                    continue

                h = hashlib.sha256(normalized.encode()).hexdigest()
                hash_groups[h].append((pf, func))

        # Emit findings for groups meeting the threshold
        findings: list[Finding] = []
        for group in hash_groups.values():
            if len(group) < self.threshold:
                continue

            for pf, func in group:
                others = [
                    f"{f.name} ({str(p.path)}:{f.line})"
                    for p, f in group
                    if not (p is pf and f is func)
                ]
                related = [
                    str(p.path) for p, f in group if not (p is pf and f is func)
                ]

                # Build snippet from first 3 lines of the function
                try:
                    src_lines = pf.path.read_text(encoding="utf-8").splitlines()
                    start = func.line - 1
                    end = min(start + 3, len(src_lines))
                    snippet = "\n".join(src_lines[start:end])
                except (OSError, UnicodeDecodeError):
                    snippet = None

                findings.append(
                    Finding(
                        detector="duplication",
                        category="duplicated_function",
                        message=(
                            f"Function '{func.name}' has {len(group) - 1} "
                            f"duplicate(s): {', '.join(others)}"
                        ),
                        file=str(pf.path),
                        line=func.line,
                        snippet=snippet,
                        related=related,
                        confidence="high",
                    )
                )

        return findings

    @staticmethod
    def _extract_body(source_lines: list[str], func: Function) -> list[str]:
        """Extract function body lines from source (1-indexed line/end_line)."""
        start = func.line - 1
        end = func.end_line
        if start < 0 or end > len(source_lines):
            return []
        return source_lines[start:end]

    @staticmethod
    def _normalize(lines: list[str]) -> str:
        """Normalize function body for structural comparison.

        Steps:
        1. Strip leading/trailing whitespace from each line
        2. Remove blank lines
        3. Remove Python comment lines (# ...)
        4. Remove JS/TS single-line comment lines (// ...)
        5. Remove block comments (/* ... */)
        6. Replace string literal contents with a placeholder
        7. Replace standalone numeric literals with 0
        8. Join everything and strip all remaining whitespace
        """
        cleaned: list[str] = []
        in_block_comment = False

        for raw_line in lines:
            line = raw_line.strip()
            if not line:
                continue

            # Handle block comments (/* ... */)
            if in_block_comment:
                if "*/" in line:
                    line = line[line.index("*/") + 2:]
                    in_block_comment = False
                    if not line.strip():
                        continue
                    line = line.strip()
                else:
                    continue

            # Remove block comment starts
            while "/*" in line:
                before = line[:line.index("/*")]
                rest = line[line.index("/*") + 2:]
                if "*/" in rest:
                    after = rest[rest.index("*/") + 2:]
                    line = before + after
                else:
                    line = before
                    in_block_comment = True
                    break

            line = line.strip()
            if not line:
                continue

            # Skip pure comment lines
            if line.startswith("#") or line.startswith("//"):
                continue

            cleaned.append(line)

        text = " ".join(cleaned)

        # Replace string contents: "..." '...' `...` with placeholder
        # Handle escaped quotes inside strings
        text = re.sub(r'"(?:[^"\\]|\\.)*"', '"_"', text)
        text = re.sub(r"'(?:[^'\\]|\\.)*'", "'_'", text)
        text = re.sub(r"`(?:[^`\\]|\\.)*`", '`_`', text)

        # Replace standalone numeric literals (int and float) with 0
        text = re.sub(r'\b\d+\.?\d*\b', '0', text)

        # Strip all whitespace
        text = re.sub(r'\s+', '', text)

        return text
