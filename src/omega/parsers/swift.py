from __future__ import annotations

import re
from pathlib import Path

from .base import Parser
from ..finding import Call, Class, Export, Function, Import, ParsedFile, Variable


class SwiftParser(Parser):
    """Parse Swift files using regex."""

    def supports(self, path: Path) -> bool:
        return path.suffix == ".swift"

    def parse_file(self, path: Path) -> ParsedFile:
        source = path.read_text(encoding="utf-8", errors="replace")
        raw_lines = source.count("\n") + (1 if source and not source.endswith("\n") else 0)

        try:
            lines = source.splitlines()
            cleaned = self._strip_comments(lines)

            imports = self._parse_imports(cleaned)
            functions = self._parse_functions(cleaned)
            classes = self._parse_classes(cleaned)
            variables = self._parse_variables(cleaned)
            calls = self._parse_calls(cleaned)
            exports = self._build_exports(functions, classes, variables, cleaned)

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

    def _strip_comments(self, lines: list[str]) -> list[str]:
        """Strip // and /* ... */ comments."""
        result: list[str] = []
        in_block = False
        for line in lines:
            if in_block:
                end_idx = line.find("*/")
                if end_idx != -1:
                    in_block = False
                    line = line[end_idx + 2:]
                else:
                    result.append("")
                    continue

            line = re.sub(r'/\*.*?\*/', '', line)
            block_start = line.find("/*")
            if block_start != -1:
                in_block = True
                line = line[:block_start]
            line = re.sub(r'//.*$', '', line)
            result.append(line)
        return result

    # ── imports ───────────────────────────────────────────────────

    def _parse_imports(self, lines: list[str]) -> list[Import]:
        imports: list[Import] = []
        _import = re.compile(r'(?:@testable\s+)?import\s+(\w[\w.]*)')

        for i, line in enumerate(lines):
            m = _import.search(line.strip())
            if m:
                name = m.group(1)
                imports.append(Import(
                    name=name,
                    source=name,
                    line=i + 1,
                ))
        return imports

    # ── functions ─────────────────────────────────────────────────

    def _parse_functions(self, lines: list[str]) -> list[Function]:
        functions: list[Function] = []
        _func = re.compile(
            r'(?:(?:public|private|fileprivate|internal|open)\s+)?'
            r'(?:(?:static|class|override|mutating|final)\s+)*'
            r'func\s+(\w+)\s*(?:<[^>]*>)?\s*\(([^)]*)\)'
        )
        _init = re.compile(
            r'(?:(?:public|private|fileprivate|internal|open)\s+)?'
            r'(?:(?:required|convenience|override)\s+)*'
            r'(init)\s*(?:\?|!)?\s*\(([^)]*)\)'
        )
        _deinit = re.compile(
            r'(?:(?:public|private|fileprivate|internal|open)\s+)?'
            r'(deinit)\s*\{'
        )

        for i, line in enumerate(lines):
            stripped = line.strip()
            m = _func.search(stripped)
            if m:
                name = m.group(1)
                params = m.group(2)
                param_count = self._count_params(params)
                end_line = self._find_brace_end(lines, i)
                functions.append(Function(
                    name=name,
                    line=i + 1,
                    end_line=end_line + 1,
                    param_count=param_count,
                    line_count=end_line - i + 1,
                ))
                continue
            m = _init.search(stripped)
            if m:
                name = m.group(1)
                params = m.group(2)
                param_count = self._count_params(params)
                end_line = self._find_brace_end(lines, i)
                functions.append(Function(
                    name=name,
                    line=i + 1,
                    end_line=end_line + 1,
                    param_count=param_count,
                    line_count=end_line - i + 1,
                ))
                continue
            m = _deinit.search(stripped)
            if m:
                end_line = self._find_brace_end(lines, i)
                functions.append(Function(
                    name="deinit",
                    line=i + 1,
                    end_line=end_line + 1,
                    param_count=0,
                    line_count=end_line - i + 1,
                ))
        return functions

    def _count_params(self, params_str: str) -> int:
        params_str = params_str.strip()
        if not params_str:
            return 0
        return len([p for p in params_str.split(",") if p.strip()])

    def _find_brace_end(self, lines: list[str], start: int) -> int:
        """Find the closing brace matching the first opening brace at or after `start`."""
        depth = 0
        found_open = False
        for i in range(start, len(lines)):
            for ch in lines[i]:
                if ch == "{":
                    depth += 1
                    found_open = True
                elif ch == "}":
                    depth -= 1
                    if found_open and depth == 0:
                        return i
        return start

    # ── classes ───────────────────────────────────────────────────

    def _parse_classes(self, lines: list[str]) -> list[Class]:
        classes: list[Class] = []
        _class = re.compile(
            r'(?:(?:public|private|fileprivate|internal|open)\s+)?'
            r'(?:(?:final)\s+)?'
            r'(?:class|struct|enum|protocol|actor|extension)\s+(\w+)'
        )
        _method = re.compile(r'\bfunc\s+\w+|(?:required\s+|convenience\s+|override\s+)*init\s*[\((?]')

        for i, line in enumerate(lines):
            m = _class.search(line.strip())
            if m:
                name = m.group(1)
                end_line = self._find_brace_end(lines, i)
                method_count = 0
                for j in range(i + 1, end_line):
                    if _method.search(lines[j]):
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
        _property = re.compile(
            r'(?:(?:public|private|fileprivate|internal|open)\s+)?'
            r'(?:(?:static|class|lazy|weak|unowned)\s+)*'
            r'(?:var|let)\s+(\w+)'
        )

        for i, line in enumerate(lines):
            stripped = line.strip()
            # Skip lines inside function bodies (rough heuristic: skip if inside func)
            if re.match(r'(?:func|init|deinit)\b', stripped):
                continue
            m = _property.search(stripped)
            if m:
                name = m.group(1)
                # Determine scope based on indentation heuristic
                indent = len(line) - len(line.lstrip())
                scope = "class" if indent > 0 else "module"
                variables.append(Variable(
                    name=name, line=i + 1, scope=scope,
                ))
        return variables

    # ── exports ───────────────────────────────────────────────────

    def _build_exports(
        self,
        functions: list[Function],
        classes: list[Class],
        variables: list[Variable],
        lines: list[str],
    ) -> list[Export]:
        """public/open are exported. private/fileprivate are not.
        Default (internal) is treated as exported within module."""
        exports: list[Export] = []
        _private = re.compile(r'(?:^|\s)(?:private|fileprivate)\s')

        for fn in functions:
            line_text = lines[fn.line - 1] if fn.line - 1 < len(lines) else ""
            if not _private.search(line_text):
                exports.append(Export(name=fn.name, line=fn.line, kind="function"))
        for cls in classes:
            line_text = lines[cls.line - 1] if cls.line - 1 < len(lines) else ""
            if not _private.search(line_text):
                exports.append(Export(name=cls.name, line=cls.line, kind="class"))
        for var in variables:
            line_text = lines[var.line - 1] if var.line - 1 < len(lines) else ""
            if not _private.search(line_text):
                exports.append(Export(name=var.name, line=var.line, kind="variable"))
        return exports

    # ── calls ─────────────────────────────────────────────────────

    def _parse_calls(self, lines: list[str]) -> list[Call]:
        calls: list[Call] = []
        _call = re.compile(r'([\w.]+)\s*\(')
        _keywords = {
            "func", "if", "else", "guard", "for", "while", "switch",
            "case", "return", "throw", "try", "catch", "import", "let",
            "var", "class", "struct", "enum", "protocol", "actor",
            "extension", "init", "deinit", "where", "in", "as", "is",
            "print", "debugPrint",
        }

        for i, line in enumerate(lines):
            for m in _call.finditer(line):
                name = m.group(1)
                base = name.split(".")[-1] if "." in name else name
                if base not in _keywords:
                    calls.append(Call(name=name, line=i + 1))
        return calls
