from __future__ import annotations

from pathlib import Path

from .base import Detector
from ..finding import Finding, ParsedFile


# Dunder methods and special names that should never be flagged
_SKIP_FUNCTION_NAMES = {
    "main",
    "__init__",
    "__str__",
    "__repr__",
    "__eq__",
    "__hash__",
    "__len__",
    "__iter__",
    "__next__",
    "__enter__",
    "__exit__",
    "__call__",
    "__getattr__",
    "__setattr__",
    "__delattr__",
    "__getitem__",
    "__setitem__",
    "__delitem__",
    "__contains__",
    "__bool__",
    "__lt__",
    "__le__",
    "__gt__",
    "__ge__",
    "__ne__",
    "__add__",
    "__sub__",
    "__mul__",
    "__truediv__",
    "__floordiv__",
    "__mod__",
    "__pow__",
    "__and__",
    "__or__",
    "__xor__",
    "__invert__",
    "__neg__",
    "__pos__",
    "__abs__",
    "__new__",
    "__del__",
    "__format__",
    "__repr__",
}

# Next.js special exports that are called by the framework, not user code
_NEXTJS_SPECIAL_EXPORTS = {
    "getServerSideProps",
    "getStaticProps",
    "getStaticPaths",
    "generateStaticParams",
    "generateMetadata",
    "metadata",
    "revalidate",
    "dynamic",
    "runtime",
    "preferredRegion",
    "fetchCache",
    "dynamicParams",
}

# Python dunder variables that should never be flagged
_SKIP_VARIABLE_NAMES = {
    "__all__",
    "__version__",
    "__name__",
    "__file__",
    "__doc__",
    "__package__",
    "__spec__",
    "__loader__",
    "__path__",
    "__builtins__",
    "__cached__",
    "__author__",
}


def _is_pascal_case(name: str) -> bool:
    """Check if a name is PascalCase (likely a React component)."""
    return bool(name) and name[0].isupper() and not name.isupper() and "_" not in name


def _is_all_caps(name: str) -> bool:
    """Check if a name is ALL_CAPS (likely a constant)."""
    return name == name.upper() and len(name) > 1 and name.replace("_", "").isalpha()


def _read_snippet(path: Path, line: int, count: int = 4) -> str | None:
    """Read a few lines from a file starting at the given line number."""
    try:
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines()
        start = max(0, line - 1)  # 0-based
        end = min(len(lines), start + count)
        return "\n".join(lines[start:end])
    except (OSError, UnicodeDecodeError):
        return None


def _normalize_module_path(source: str, importer_path: Path) -> str | None:
    """Try to resolve a relative import source to a comparable path stem.

    For example, './utils' imported from 'src/components/Button.tsx'
    should resolve to something we can compare against a file at
    'src/utils.ts'.

    Returns a simplified path string (without extension) or None.
    """
    if not source:
        return None

    # Relative imports: ./foo, ../foo, etc.
    if source.startswith("."):
        parent = importer_path.parent
        # Walk up for each ..
        parts = source.split("/")
        for part in parts:
            if part == ".":
                continue
            elif part == "..":
                parent = parent.parent
            else:
                parent = parent / part
        return str(parent)

    # Absolute/package imports: just return as-is for matching
    return source


def _file_matches_source(file_path: Path, resolved_source: str) -> bool:
    """Check if a file path could be the target of an import source."""
    # Strip extensions for comparison
    file_stem = str(file_path)
    for ext in (".ts", ".tsx", ".js", ".jsx", ".py", ".mjs", ".cjs"):
        if file_stem.endswith(ext):
            file_stem = file_stem[: -len(ext)]
            break

    # Also handle index files: src/utils/index -> src/utils
    if file_stem.endswith("/index"):
        file_stem_alt = file_stem[: -len("/index")]
    else:
        file_stem_alt = None

    return (
        file_stem == resolved_source
        or file_stem.endswith("/" + resolved_source)
        or resolved_source.endswith("/" + Path(file_stem).name)
        or (file_stem_alt is not None and (
            file_stem_alt == resolved_source
            or file_stem_alt.endswith("/" + resolved_source)
        ))
    )


class DeadCodeDetector(Detector):
    name = "dead_code"

    def run(self, files: list[ParsedFile]) -> list[Finding]:
        findings: list[Finding] = []

        # ---- Phase 1: Build global usage index ----

        # All call names across every file, mapped to which files reference them
        # call_name -> set of file paths that use it
        global_calls: dict[str, set[str]] = {}
        # import_name -> set of file paths that import it
        global_imports: dict[str, set[str]] = {}
        # All names referenced anywhere (calls + imports + variables)
        global_names: set[str] = set()
        # Per-file call names
        per_file_calls: dict[str, set[str]] = {}
        # exported names per file
        per_file_exports: dict[str, set[str]] = {}
        # What each file imports: file_path -> list of (name, source)
        per_file_import_details: dict[str, list[tuple[str, str | None]]] = {}
        # Functions that have decorators (by file_path:func_name)
        decorated_functions: set[str] = set()

        for pf in files:
            fp = str(pf.path)
            file_calls: set[str] = set()

            for call in pf.calls:
                file_calls.add(call.name)
                global_names.add(call.name)
                # Split dotted names: "foo.bar.baz" -> "foo", "foo.bar", "foo.bar.baz"
                parts = call.name.split(".")
                for i in range(1, len(parts) + 1):
                    partial = ".".join(parts[:i])
                    global_calls.setdefault(partial, set()).add(fp)
                    file_calls.add(partial)
                    global_names.add(partial)
                # Strip self./cls. prefix for method calls
                for prefix in ("self.", "cls."):
                    if call.name.startswith(prefix):
                        stripped = call.name[len(prefix):]
                        file_calls.add(stripped)
                        global_calls.setdefault(stripped, set()).add(fp)
                        global_names.add(stripped)

            per_file_calls[fp] = file_calls

            for imp in pf.imports:
                local_name = imp.alias if imp.alias else imp.name
                global_imports.setdefault(local_name, set()).add(fp)
                global_names.add(local_name)
                # Also add the raw import name
                global_names.add(imp.name)
                global_imports.setdefault(imp.name, set()).add(fp)

            per_file_import_details[fp] = [
                (imp.alias if imp.alias else imp.name, imp.source)
                for imp in pf.imports
            ]

            export_names: set[str] = set()
            for exp in pf.exports:
                export_names.add(exp.name)
            per_file_exports[fp] = export_names

            # Collect variable references as names too
            for var in pf.variables:
                global_names.add(var.name)

            # Detect decorated functions by checking if the line before
            # the function def starts with @ in the source
            try:
                source_lines = pf.path.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeDecodeError):
                source_lines = []

            for func in pf.functions:
                # Check lines above the function definition for decorators
                line_idx = func.line - 1  # 0-based
                check_line = line_idx - 1
                while check_line >= 0:
                    stripped = source_lines[check_line].strip() if check_line < len(source_lines) else ""
                    if stripped.startswith("@"):
                        decorated_functions.add(f"{fp}:{func.name}")
                        break
                    elif stripped == "" or stripped.startswith("#"):
                        # Skip blank lines and comments above
                        check_line -= 1
                        continue
                    else:
                        break

        # ---- Phase 2: Detect unused definitions ----

        for pf in files:
            fp = str(pf.path)
            file_export_names = per_file_exports.get(fp, set())
            filename = pf.path.name

            # --- Unused Functions ---
            for func in pf.functions:
                # Skip special names
                if func.name in _SKIP_FUNCTION_NAMES:
                    continue
                if func.name.startswith("test_"):
                    continue
                if func.name.startswith("_") and not func.name.startswith("__"):
                    # Private functions: only flag if unused in own file
                    pass

                # Skip decorated functions
                if f"{fp}:{func.name}" in decorated_functions:
                    continue

                # Skip exported functions (public API)
                if func.name in file_export_names:
                    continue

                # Skip React components (PascalCase)
                if _is_pascal_case(func.name):
                    continue

                # Skip Next.js special exports
                if func.name in _NEXTJS_SPECIAL_EXPORTS:
                    continue

                # Check if referenced in other files
                other_file_refs = set()
                if func.name in global_calls:
                    other_file_refs = global_calls[func.name] - {fp}
                if func.name in global_imports:
                    other_file_refs |= global_imports[func.name] - {fp}

                # Check if called within own file (outside its own definition)
                own_file_calls = per_file_calls.get(fp, set())
                called_in_own_file = func.name in own_file_calls

                if other_file_refs:
                    # Used in other files, not dead
                    continue

                if called_in_own_file:
                    # Used in own file but nowhere else. Could still be fine.
                    # Only flag with medium confidence if it's not exported.
                    # Actually, this is usually legitimate (helper used in same file).
                    # Be conservative: skip it.
                    continue

                # Zero references anywhere (or only self-recursive)
                snippet = _read_snippet(pf.path, func.line)
                findings.append(
                    Finding(
                        detector="dead_code",
                        category="unused_function",
                        message=f"Function '{func.name}' is defined but never called",
                        file=fp,
                        line=func.line,
                        snippet=snippet,
                        confidence="high",
                    )
                )

            # --- Unused Classes ---
            for cls_def in pf.classes:
                if cls_def.name.startswith("_") and not cls_def.name.startswith("__"):
                    pass  # private, check anyway

                # Skip exported classes
                if cls_def.name in file_export_names:
                    continue

                # Check global references
                other_file_refs = set()
                if cls_def.name in global_calls:
                    other_file_refs = global_calls[cls_def.name] - {fp}
                if cls_def.name in global_imports:
                    other_file_refs |= global_imports[cls_def.name] - {fp}

                own_file_calls = per_file_calls.get(fp, set())
                called_in_own_file = cls_def.name in own_file_calls

                if other_file_refs or called_in_own_file:
                    continue

                snippet = _read_snippet(pf.path, cls_def.line)
                findings.append(
                    Finding(
                        detector="dead_code",
                        category="unused_class",
                        message=f"Class '{cls_def.name}' is defined but never referenced",
                        file=fp,
                        line=cls_def.line,
                        snippet=snippet,
                        confidence="high",
                    )
                )

            # --- Unused Exports (JS/TS only - Python "exports" are just public names) ---
            if pf.path.suffix in (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"):
              for exp in pf.exports:
                # Skip Next.js page/layout/route default exports
                if exp.kind == "default" and filename in (
                    "page.tsx", "page.jsx", "page.js",
                    "layout.tsx", "layout.jsx", "layout.js",
                    "route.ts", "route.js",
                    "loading.tsx", "loading.jsx",
                    "error.tsx", "error.jsx",
                    "not-found.tsx", "not-found.jsx",
                    "template.tsx", "template.jsx",
                ):
                    continue

                # Skip barrel exports (index files)
                if filename in ("index.ts", "index.js", "index.tsx", "index.jsx", "index.mjs"):
                    continue

                # Skip config files consumed by frameworks/tools, not imported by code
                if filename in (
                    "next.config.js", "next.config.mjs", "next.config.ts",
                    "tailwind.config.js", "tailwind.config.ts",
                    "postcss.config.js", "postcss.config.mjs",
                    "vite.config.ts", "vite.config.js",
                    "jest.config.js", "jest.config.ts",
                    "vitest.config.ts", "vitest.config.js",
                    "eslint.config.js", "eslint.config.mjs",
                    ".eslintrc.js", "tsconfig.json",
                    "drizzle.config.ts", "knexfile.js",
                ):
                    continue

                # Skip Next.js special export names
                if exp.name in _NEXTJS_SPECIAL_EXPORTS:
                    continue

                # Check if any other file imports this name from this file's module
                imported_elsewhere = False
                for other_fp, import_list in per_file_import_details.items():
                    if other_fp == fp:
                        continue
                    for imp_name, imp_source in import_list:
                        if imp_name != exp.name:
                            continue
                        if imp_source is None:
                            continue
                        # Try to resolve the import source and match to this file
                        other_path = None
                        for f in files:
                            if str(f.path) == other_fp:
                                other_path = f.path
                                break
                        if other_path is None:
                            continue
                        resolved = _normalize_module_path(imp_source, other_path)
                        if resolved and _file_matches_source(pf.path, resolved):
                            imported_elsewhere = True
                            break
                    if imported_elsewhere:
                        break

                # Fallback: check if the export name appears in global imports at all
                if not imported_elsewhere and exp.name in global_imports:
                    other_importers = global_imports[exp.name] - {fp}
                    if other_importers:
                        # Name is imported somewhere, might be from this file.
                        # Be conservative: don't flag.
                        imported_elsewhere = True

                if imported_elsewhere:
                    continue

                snippet = _read_snippet(pf.path, exp.line)
                findings.append(
                    Finding(
                        detector="dead_code",
                        category="unused_export",
                        message=f"Export '{exp.name}' is not imported by any other file",
                        file=fp,
                        line=exp.line,
                        snippet=snippet,
                        confidence="high",
                    )
                )

            # --- Unused Variables (module-level only) ---
            for var in pf.variables:
                if var.scope != "module":
                    continue

                # Skip underscore-prefixed (intentionally unused)
                if var.name.startswith("_"):
                    continue

                # Skip Python dunder variables
                if var.name in _SKIP_VARIABLE_NAMES:
                    continue

                # Skip ALL_CAPS constants
                if _is_all_caps(var.name):
                    continue

                # Skip exported variables
                if var.name in file_export_names:
                    continue

                # Check if referenced anywhere
                other_file_refs = set()
                if var.name in global_calls:
                    other_file_refs = global_calls[var.name] - {fp}
                if var.name in global_imports:
                    other_file_refs |= global_imports[var.name] - {fp}

                own_file_calls = per_file_calls.get(fp, set())
                used_in_own_file = var.name in own_file_calls

                if other_file_refs or used_in_own_file:
                    continue

                # Zero references: check file text as fallback (the name might
                # appear in contexts not captured by the call list)
                try:
                    source_text = pf.path.read_text(encoding="utf-8")
                    source_lines = source_text.splitlines()
                except (OSError, UnicodeDecodeError):
                    continue

                # Count occurrences of the variable name in the file
                # (beyond its own definition line)
                var_line_idx = var.line - 1
                found_elsewhere = False
                for idx, line in enumerate(source_lines):
                    if idx == var_line_idx:
                        continue
                    if var.name in line:
                        found_elsewhere = True
                        break

                if found_elsewhere:
                    # Referenced in file text but not in structured calls.
                    # Could be type annotation, string, etc. Be conservative.
                    continue

                snippet = _read_snippet(pf.path, var.line, count=2)
                findings.append(
                    Finding(
                        detector="dead_code",
                        category="unused_variable",
                        message=f"Module-level variable '{var.name}' is never referenced",
                        file=fp,
                        line=var.line,
                        snippet=snippet,
                        confidence="high",
                    )
                )

        return findings
