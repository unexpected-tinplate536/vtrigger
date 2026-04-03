from __future__ import annotations

from .base import Detector
from ..finding import Finding, ParsedFile


class UnusedImportsDetector(Detector):
    name = "unused_imports"

    def run(self, files: list[ParsedFile]) -> list[Finding]:
        findings: list[Finding] = []

        for pf in files:
            if not pf.imports:
                continue

            # Read file text for fallback matching
            try:
                file_text = pf.path.read_text(encoding="utf-8")
                file_lines = file_text.splitlines()
            except (OSError, UnicodeDecodeError):
                continue

            # Collect used names from structured data
            used_names: set[str] = set()

            # From calls: "os.path.join" -> "os", "os.path", "os.path.join"
            for call in pf.calls:
                used_names.add(call.name)
                parts = call.name.split(".")
                for i in range(1, len(parts)):
                    used_names.add(".".join(parts[:i]))

            # From functions (name itself, as it could be a re-export or decorator target)
            for func in pf.functions:
                used_names.add(func.name)

            # From classes
            for cls in pf.classes:
                used_names.add(cls.name)

            # From variables
            for var in pf.variables:
                used_names.add(var.name)

            # Check each import
            for imp in pf.imports:
                local_name = imp.alias if imp.alias else imp.name

                # Skip __future__ imports (they modify compiler behavior, not used as names)
                if imp.source == "__future__":
                    continue

                # Skip side-effect imports (import with no name, e.g., import 'styles.css')
                if not local_name:
                    continue

                # Check structured usage first
                if local_name in used_names:
                    continue

                # Fallback: check if the name appears in file text beyond
                # its own import line. This catches type annotations, string
                # references, decorators, f-strings, comments, etc.
                import_line_idx = imp.line - 1  # 0-based
                found_elsewhere = False
                for idx, line in enumerate(file_lines):
                    if idx == import_line_idx:
                        continue
                    if local_name in line:
                        found_elsewhere = True
                        break

                if found_elsewhere:
                    continue

                # Build snippet from the import line
                snippet = None
                if 0 <= import_line_idx < len(file_lines):
                    snippet = file_lines[import_line_idx]

                findings.append(
                    Finding(
                        detector="unused_imports",
                        category="unused_import",
                        message=f"'{local_name}' is imported but never used",
                        file=str(pf.path),
                        line=imp.line,
                        snippet=snippet,
                        confidence="high",
                    )
                )

        return findings
