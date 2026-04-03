from __future__ import annotations

from .base import Detector
from ..config import Config
from ..finding import Finding, ParsedFile


class SizeDetector(Detector):
    name = "size"

    def __init__(self, config: Config) -> None:
        self.config = config

    def run(self, files: list[ParsedFile]) -> list[Finding]:
        findings: list[Finding] = []

        for pf in files:
            # Check file size
            if pf.raw_lines > self.config.thresholds.max_file_lines:
                findings.append(
                    Finding(
                        detector="size",
                        category="large_file",
                        message=f"File is {pf.raw_lines} lines (threshold: {self.config.thresholds.max_file_lines})",
                        file=str(pf.path),
                        line=1,
                        snippet=None,
                        confidence="high",
                    )
                )

            # Read file lines for snippets
            file_lines: list[str] | None = None
            try:
                file_lines = pf.path.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeDecodeError):
                file_lines = None

            # Check function sizes
            for func in pf.functions:
                if func.line_count > self.config.thresholds.max_function_lines:
                    snippet = None
                    if file_lines is not None:
                        start = func.line - 1  # 0-based
                        snippet = "\n".join(file_lines[start : start + 3])

                    findings.append(
                        Finding(
                            detector="size",
                            category="large_function",
                            message=f"Function '{func.name}' is {func.line_count} lines (threshold: {self.config.thresholds.max_function_lines})",
                            file=str(pf.path),
                            line=func.line,
                            snippet=snippet,
                            confidence="high",
                        )
                    )

            # Check class method counts
            for cls in pf.classes:
                if cls.method_count >= self.config.thresholds.max_class_methods:
                    snippet = None
                    if file_lines is not None:
                        start = cls.line - 1  # 0-based
                        snippet = "\n".join(file_lines[start : start + 3])

                    findings.append(
                        Finding(
                            detector="size",
                            category="god_class",
                            message=f"Class '{cls.name}' has {cls.method_count} methods (threshold: {self.config.thresholds.max_class_methods})",
                            file=str(pf.path),
                            line=cls.line,
                            snippet=snippet,
                            confidence="high",
                        )
                    )

        return findings
