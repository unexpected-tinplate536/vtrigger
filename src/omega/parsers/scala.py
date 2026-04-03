from __future__ import annotations

import re
from pathlib import Path

from .base import Parser
from ..finding import Call, Class, Export, Function, Import, ParsedFile, Variable


class ScalaParser(Parser):
    """Parse Scala files using regex-based parsing."""

    def supports(self, path: Path) -> bool:
        return path.suffix in (".scala", ".sc")

    def parse_file(self, path: Path) -> ParsedFile:
        source = path.read_text(encoding="utf-8", errors="replace")
        raw_lines = source.count("\n") + (1 if source and not source.endswith("\n") else 0)

        try:
            return self._parse(path, source, raw_lines)
        except Exception:
            return ParsedFile(path=path, raw_lines=raw_lines)

    def _parse(self, path: Path, source: str, raw_lines: int) -> ParsedFile:
        cleaned = self._strip_comments(source)
        lines = cleaned.splitlines()

        imports = self._parse_imports(lines)
        functions = self._parse_functions(lines)
        classes = self._parse_classes(lines)
        variables = self._parse_variables(lines)
        calls = self._parse_calls(lines)
        exports = self._build_exports(functions, classes, variables)

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

    def _strip_comments(self, source: str) -> str:
        # Remove block comments /* ... */ (non-greedy, handles multiline)
        source = re.sub(r'/\*.*?\*/', '', source, flags=re.DOTALL)
        # Remove line comments // ...
        source = re.sub(r'//[^\n]*', '', source)
        return source

    # ── imports ───────────────────────────────────────────────────

    def _parse_imports(self, lines: list[str]) -> list[Import]:
        imports: list[Import] = []
        _single = re.compile(
            r'^import\s+([\w.]+)\.(\{[^}]+\}|_|\w+)'
        )
        in_block = False
        block_base = ""

        for i, line in enumerate(lines):
            stripped = line.strip()

            # Handle multi-line import block: import foo.bar.{A, B}
            # or import block started with just "import {"
            if in_block:
                if "}" in stripped:
                    in_block = False
                # Parse names inside the block
                for name_match in re.finditer(r'(\w+)(?:\s*=>\s*(\w+))?', stripped):
                    if name_match.group(1) == "}":
                        continue
                    name = name_match.group(1)
                    alias = name_match.group(2)
                    if name in ("import", "{", "}"):
                        continue
                    imports.append(Import(
                        name=name,
                        source=block_base,
                        line=i + 1,
                        alias=alias,
                    ))
                continue

            m = _single.match(stripped)
            if m:
                base_path = m.group(1)
                imported = m.group(2)

                if imported == "_":
                    # Wildcard import: import foo.bar._
                    imports.append(Import(
                        name="_",
                        source=base_path,
                        line=i + 1,
                    ))
                elif imported.startswith("{"):
                    # Brace import, might be single line or start of multiline
                    if "}" in imported:
                        # Single line: import foo.{Bar, Baz}
                        inner = imported.strip("{}")
                        for part in inner.split(","):
                            part = part.strip()
                            if not part:
                                continue
                            rename = re.match(r'(\w+)\s*=>\s*(\w+)', part)
                            if rename:
                                imports.append(Import(
                                    name=rename.group(1),
                                    source=base_path,
                                    line=i + 1,
                                    alias=rename.group(2),
                                ))
                            else:
                                imports.append(Import(
                                    name=part,
                                    source=base_path,
                                    line=i + 1,
                                ))
                    else:
                        # Multi-line brace import
                        in_block = True
                        block_base = base_path
                        inner = imported.lstrip("{").strip()
                        if inner:
                            for part in inner.split(","):
                                part = part.strip()
                                if not part:
                                    continue
                                imports.append(Import(
                                    name=part,
                                    source=base_path,
                                    line=i + 1,
                                ))
                else:
                    # Simple import: import foo.bar.Baz
                    imports.append(Import(
                        name=imported,
                        source=base_path,
                        line=i + 1,
                    ))

        return imports

    # ── functions ─────────────────────────────────────────────────

    def _parse_functions(self, lines: list[str]) -> list[Function]:
        functions: list[Function] = []
        _func = re.compile(
            r'^(?:(?:private|protected|override|final|abstract|implicit|inline)\s+)*'
            r'def\s+(\w+)\s*'
            r'(?:\[.*?\])?\s*'  # type params
            r'(?:\((.*?)\))?'   # params (optional for no-paren defs)
        )

        for i, line in enumerate(lines):
            stripped = line.strip()
            m = _func.match(stripped)
            if m:
                name = m.group(1)
                params_str = m.group(2)
                param_count = self._count_params(params_str) if params_str else 0

                # Determine end line: brace counting or expression body
                if "{" in line:
                    end_line = self._find_brace_end(lines, i)
                elif "=" in stripped and "{" not in stripped:
                    # Expression body: def foo = expr
                    end_line = i
                else:
                    # Look ahead for opening brace
                    end_line = self._find_brace_end(lines, i)

                functions.append(Function(
                    name=name,
                    line=i + 1,
                    end_line=end_line + 1,
                    param_count=param_count,
                    line_count=end_line - i + 1,
                ))

        return functions

    def _count_params(self, params_str: str) -> int:
        if not params_str or not params_str.strip():
            return 0
        # Handle nested parens/brackets in type signatures
        depth = 0
        count = 1
        for ch in params_str:
            if ch in ("(", "[", "{"):
                depth += 1
            elif ch in (")", "]", "}"):
                depth -= 1
            elif ch == "," and depth == 0:
                count += 1
        return count

    def _find_brace_end(self, lines: list[str], start: int) -> int:
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

    # ── classes ───────────────────────────────────────────────────

    def _parse_classes(self, lines: list[str]) -> list[Class]:
        classes: list[Class] = []
        _class = re.compile(
            r'^(?:(?:private|protected|sealed|abstract|final|implicit|lazy|case)\s+)*'
            r'(class|object|trait|enum)\s+(\w+)'
        )
        _method = re.compile(
            r'^\s+(?:(?:private|protected|override|final|abstract|implicit|inline)\s+)*'
            r'def\s+\w+'
        )

        for i, line in enumerate(lines):
            stripped = line.strip()
            m = _class.match(stripped)
            if m:
                name = m.group(2)
                if "{" in line:
                    end_line = self._find_brace_end(lines, i)
                else:
                    end_line = self._find_brace_end(lines, i)

                # Count methods inside the class body
                method_count = 0
                for j in range(i + 1, min(end_line + 1, len(lines))):
                    if _method.match(lines[j]):
                        method_count += 1

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
            r'^(?:(?:private|protected|override|final|implicit|lazy)\s+)*'
            r'(val|var)\s+(\w+)'
        )

        for i, line in enumerate(lines):
            stripped = line.strip()
            m = _var.match(stripped)
            if m:
                name = m.group(2)
                # Only module/class level (not indented too deeply, heuristic)
                indent = len(line) - len(line.lstrip())
                scope = "module" if indent <= 2 else "class"
                variables.append(Variable(
                    name=name,
                    line=i + 1,
                    scope=scope,
                ))

        return variables

    # ── calls ─────────────────────────────────────────────────────

    def _parse_calls(self, lines: list[str]) -> list[Call]:
        calls: list[Call] = []
        # Standard calls: foo(), obj.bar(), Foo.apply(), new Foo()
        _call = re.compile(r'(?:new\s+)?([\w.]+)\s*\(')
        # Infix calls: x :: xs, x +: xs (operator-style)
        _infix = re.compile(r'\w+\s+(::|\+:|\+\+|::\:)\s+\w+')
        _keywords = {
            "if", "else", "for", "while", "match", "case", "return",
            "yield", "throw", "try", "catch", "finally", "class", "trait",
            "object", "def", "val", "var", "import", "package", "extends",
            "with", "new", "type", "sealed", "abstract", "override",
            "private", "protected", "implicit", "lazy", "enum",
        }

        for i, line in enumerate(lines):
            for m in _call.finditer(line):
                name = m.group(1)
                # Handle "new Foo" -> name is "Foo"
                base = name.split(".")[-1] if "." in name else name
                if base not in _keywords:
                    # Check for "new" prefix in the match
                    start = m.start()
                    prefix = line[max(0, start - 4):start].strip()
                    if prefix.endswith("new"):
                        calls.append(Call(name=f"new {name}", line=i + 1))
                    else:
                        calls.append(Call(name=name, line=i + 1))

            for m in _infix.finditer(line):
                calls.append(Call(name=m.group(1), line=i + 1))

        return calls

    # ── exports ───────────────────────────────────────────────────

    def _build_exports(
        self,
        functions: list[Function],
        classes: list[Class],
        variables: list[Variable],
    ) -> list[Export]:
        """In Scala, non-private members are exported."""
        exports: list[Export] = []

        for fn in functions:
            exports.append(Export(name=fn.name, line=fn.line, kind="function"))
        for cls in classes:
            exports.append(Export(name=cls.name, line=cls.line, kind="class"))
        for var in variables:
            if var.scope == "module":
                exports.append(Export(name=var.name, line=var.line, kind="variable"))

        return exports
