from __future__ import annotations

import re
from pathlib import Path

from .base import Parser
from ..finding import Call, Class, Export, Function, Import, ParsedFile, Variable


class DartParser(Parser):
    """Parse Dart files using regex."""

    def supports(self, path: Path) -> bool:
        return path.suffix == ".dart"

    def parse_file(self, path: Path) -> ParsedFile:
        source = path.read_text(encoding="utf-8", errors="replace")
        raw_lines = source.count("\n") + (1 if source and not source.endswith("\n") else 0)

        try:
            cleaned = self._strip_comments(source)
            lines = cleaned.splitlines()

            imports = self._parse_imports(lines)
            functions = self._parse_functions(lines)
            classes = self._parse_classes(lines, functions)
            variables = self._parse_variables(lines)
            calls = self._parse_calls(lines)
            exports = self._build_exports(imports, functions, classes, variables, lines)

            return ParsedFile(
                path=path,
                imports=imports,
                exports=exports,
                functions=functions,
                classes=classes,
                variables=variables,
                calls=calls,
                raw_lines=raw_lines,
            )
        except Exception:
            return ParsedFile(path=path, raw_lines=raw_lines)

    # ── comment stripping ─────────────────────────────────────────

    def _strip_comments(self, source: str) -> str:
        # Remove block comments /* ... */
        source = re.sub(r'/\*.*?\*/', '', source, flags=re.DOTALL)
        # Remove line comments // ...
        source = re.sub(r'//[^\n]*', '', source)
        return source

    # ── imports ───────────────────────────────────────────────────

    def _parse_imports(self, lines: list[str]) -> list[Import]:
        imports: list[Import] = []
        _import = re.compile(
            r"^(?:import|export|part(?:\s+of)?)\s+"
            r"'([^']+)'"
            r"(?:\s+as\s+(\w+))?"
            r"(?:\s+(?:show|hide)\s+([\w,\s]+))?"
            r"\s*;"
        )

        for i, line in enumerate(lines):
            m = _import.match(line.strip())
            if m:
                source = m.group(1)
                alias = m.group(2)
                # Derive a short name from the source path
                short = source.rsplit("/", 1)[-1]
                if short.endswith(".dart"):
                    short = short[:-5]
                imports.append(Import(
                    name=short,
                    source=source,
                    line=i + 1,
                    alias=alias,
                ))
        return imports

    # ── functions ─────────────────────────────────────────────────

    def _parse_functions(self, lines: list[str]) -> list[Function]:
        functions: list[Function] = []
        # Matches: optional modifiers, optional return type, function name, params
        _func = re.compile(
            r'^(?:(?:static|external|abstract|override|async)\s+)*'
            r'(?:(?:[\w<>,\s\?]+)\s+)?'  # return type (optional)
            r'(?:(?:get|set)\s+)?'         # getter/setter
            r'(\w+)\s*'                     # function name
            r'\(([^)]*)\)'                  # params
            r'\s*(?:async\s*)?'             # optional async after params
            r'(?:\{|=>|;)'                  # body start or arrow or abstract
        )
        # Getter with => (no parens)
        _getter = re.compile(
            r'^(?:(?:static|external|abstract|override)\s+)*'
            r'(?:(?:[\w<>,\s\?]+)\s+)?'
            r'get\s+(\w+)\s*(?:=>|\{)'
        )

        _keywords = {
            "if", "else", "for", "while", "switch", "catch", "return",
            "throw", "assert", "await", "yield", "import", "export",
            "class", "enum", "mixin", "extension", "typedef",
        }

        for i, line in enumerate(lines):
            stripped = line.strip()
            if not stripped:
                continue

            # Try getter first
            gm = _getter.match(stripped)
            if gm:
                name = gm.group(1)
                if name not in _keywords:
                    end_line = self._find_end_brace(lines, i) if "{" in stripped else i
                    functions.append(Function(
                        name=name,
                        line=i + 1,
                        end_line=end_line + 1,
                        param_count=0,
                        line_count=end_line - i + 1,
                    ))
                continue

            m = _func.match(stripped)
            if m:
                name = m.group(1)
                params = m.group(2)
                if name in _keywords:
                    continue
                param_count = self._count_params(params)
                if "=>" in stripped and "{" not in stripped.split("=>", 1)[0]:
                    # Single expression function with =>
                    # Find end by looking for the semicolon
                    end_line = self._find_arrow_end(lines, i)
                elif "{" in stripped:
                    end_line = self._find_end_brace(lines, i)
                else:
                    # Abstract method ending with ;
                    end_line = i
                functions.append(Function(
                    name=name,
                    line=i + 1,
                    end_line=end_line + 1,
                    param_count=param_count,
                    line_count=end_line - i + 1,
                ))
        return functions

    def _count_params(self, params_str: str) -> int:
        params_str = params_str.strip()
        if not params_str:
            return 0
        # Remove nested generics/braces for counting
        cleaned = re.sub(r'<[^>]*>', '', params_str)
        cleaned = re.sub(r'\{[^}]*\}', lambda m: m.group(0).replace(',', ';'), cleaned)
        cleaned = re.sub(r'\[[^\]]*\]', lambda m: m.group(0).replace(',', ';'), cleaned)
        return len([p for p in cleaned.split(",") if p.strip()])

    def _find_end_brace(self, lines: list[str], start: int) -> int:
        depth = 0
        for i in range(start, len(lines)):
            for ch in lines[i]:
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        return i
        return start

    def _find_arrow_end(self, lines: list[str], start: int) -> int:
        for i in range(start, len(lines)):
            if ";" in lines[i]:
                return i
        return start

    # ── classes ───────────────────────────────────────────────────

    def _parse_classes(self, lines: list[str], functions: list[Function]) -> list[Class]:
        classes: list[Class] = []
        _class = re.compile(
            r'^(?:(?:abstract|sealed|base|final|interface)\s+)*'
            r'(?:class|mixin|enum|extension)\s+'
            r'(\w+)'
        )

        for i, line in enumerate(lines):
            m = _class.match(line.strip())
            if m:
                name = m.group(1)
                end_line = self._find_end_brace(lines, i)
                # Count methods: functions whose line is within this class body
                method_count = sum(
                    1 for fn in functions
                    if fn.line > i + 1 and fn.end_line <= end_line + 1
                )
                classes.append(Class(
                    name=name,
                    line=i + 1,
                    end_line=end_line + 1,
                    method_count=method_count,
                    line_count=end_line - i + 1,
                ))
        return classes

    # ── variables ─────────────────────────────────────────────────

    def _parse_variables(self, lines: list[str]) -> list[Variable]:
        variables: list[Variable] = []
        _var = re.compile(
            r'^(?:(?:static|late|external)\s+)*'
            r'(?:(?:final|const|var)\s+)'
            r'(?:(?:[\w<>,\?\s]+)\s+)?'  # optional type
            r'(\w+)\s*[=;]'
        )
        _typed_var = re.compile(
            r'^(?:(?:static|late|external)\s+)*'
            r'([\w<>,\?]+)\s+'   # type
            r'(\w+)\s*[=;]'
        )
        _keywords = {
            "class", "enum", "mixin", "extension", "import", "export",
            "if", "else", "for", "while", "switch", "return", "void",
            "abstract", "sealed", "base", "final", "interface",
            "part", "typedef", "throw", "assert", "catch", "try",
        }
        _type_keywords = {
            "return", "throw", "if", "else", "for", "while", "switch",
            "catch", "try", "class", "void", "import", "export",
            "abstract", "sealed", "base", "final", "interface",
        }

        # Track brace depth to identify top-level vs class-level
        depth = 0
        for i, line in enumerate(lines):
            stripped = line.strip()
            if not stripped:
                depth += stripped.count("{") - stripped.count("}")
                continue

            # Only process top-level (depth 0) or class-level (depth 1)
            if depth <= 1:
                m = _var.match(stripped)
                if m:
                    name = m.group(1)
                    if name not in _keywords:
                        scope = "module" if depth == 0 else "class"
                        variables.append(Variable(name=name, line=i + 1, scope=scope))
                        depth += stripped.count("{") - stripped.count("}")
                        continue

                m2 = _typed_var.match(stripped)
                if m2:
                    type_name = m2.group(1)
                    name = m2.group(2)
                    if type_name not in _type_keywords and name not in _keywords:
                        scope = "module" if depth == 0 else "class"
                        variables.append(Variable(name=name, line=i + 1, scope=scope))

            depth += stripped.count("{") - stripped.count("}")

        return variables

    # ── calls ─────────────────────────────────────────────────────

    def _parse_calls(self, lines: list[str]) -> list[Call]:
        calls: list[Call] = []
        _call = re.compile(r'(?:\.\.|\b)([\w.]+)\s*[<\w,\s>]*\(')
        _keywords = {
            "if", "else", "for", "while", "switch", "catch", "return",
            "throw", "assert", "import", "export", "class", "enum",
            "mixin", "extension", "void", "super", "this",
        }

        for i, line in enumerate(lines):
            for m in _call.finditer(line):
                name = m.group(1)
                # Clean up cascade prefix
                if name.startswith(".."):
                    name = name[2:]
                if name and name not in _keywords:
                    calls.append(Call(name=name, line=i + 1))
        return calls

    # ── exports ───────────────────────────────────────────────────

    def _build_exports(
        self,
        imports: list[Import],
        functions: list[Function],
        classes: list[Class],
        variables: list[Variable],
        lines: list[str],
    ) -> list[Export]:
        """Dart: public API is anything not starting with _ (private convention).
        Also include explicit `export` directives found in imports."""
        exports: list[Export] = []

        # Re-export directives
        for imp in imports:
            if imp.source and any(
                line.strip().startswith("export ")
                for line in lines
                if imp.source in line
            ):
                exports.append(Export(name=imp.name, line=imp.line, kind="default"))

        # Public functions (top-level only, not inside classes)
        for fn in functions:
            if not fn.name.startswith("_"):
                exports.append(Export(name=fn.name, line=fn.line, kind="function"))

        for cls in classes:
            if not cls.name.startswith("_"):
                exports.append(Export(name=cls.name, line=cls.line, kind="class"))

        for var in variables:
            if not var.name.startswith("_") and var.scope == "module":
                exports.append(Export(name=var.name, line=var.line, kind="variable"))

        return exports
