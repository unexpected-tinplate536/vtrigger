from __future__ import annotations

import re
from pathlib import Path

from .base import Parser
from ..finding import Call, Class, Export, Function, Import, ParsedFile, Variable


class PhpParser(Parser):
    """Parse PHP files using regex."""

    def supports(self, path: Path) -> bool:
        return path.suffix == ".php"

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
            exports = self._build_exports(functions, classes)

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
        """Strip // single-line and /* ... */ block comments."""
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

            # Remove /* ... */ on the same line
            line = re.sub(r'/\*.*?\*/', '', line)
            # Check for block comment start
            block_start = line.find("/*")
            if block_start != -1:
                in_block = True
                line = line[:block_start]
            # Remove single-line comments
            line = re.sub(r'//.*$', '', line)
            result.append(line)
        return result

    # ── imports ───────────────────────────────────────────────────

    def _parse_imports(self, lines: list[str]) -> list[Import]:
        imports: list[Import] = []
        _use = re.compile(r'use\s+([\w\\]+)(?:\s+as\s+(\w+))?\s*;')
        _require = re.compile(
            r"""(?:require|require_once|include|include_once)\s*[\(]?\s*['"]([^'"]+)['"]\s*[\)]?\s*;"""
        )

        for i, line in enumerate(lines):
            stripped = line.strip()
            m = _use.search(stripped)
            if m:
                full_path = m.group(1)
                alias = m.group(2)
                name = full_path.rsplit("\\", 1)[-1]
                imports.append(Import(
                    name=name,
                    source=full_path,
                    line=i + 1,
                    alias=alias,
                ))
                continue
            m = _require.search(stripped)
            if m:
                source = m.group(1)
                name = source.rsplit("/", 1)[-1]
                imports.append(Import(
                    name=name,
                    source=source,
                    line=i + 1,
                ))
        return imports

    # ── functions ─────────────────────────────────────────────────

    def _parse_functions(self, lines: list[str]) -> list[Function]:
        functions: list[Function] = []
        _func = re.compile(
            r'(?:(?:public|private|protected)\s+)?'
            r'(?:(?:static|abstract|final)\s+)*'
            r'function\s+(\w+)\s*\(([^)]*)\)'
        )

        for i, line in enumerate(lines):
            m = _func.search(line)
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
            r'(?:(?:abstract|final)\s+)?'
            r'(?:class|interface|trait|enum)\s+(\w+)'
        )
        _method = re.compile(r'\bfunction\s+\w+\s*\(')

        for i, line in enumerate(lines):
            m = _class.search(line.strip())
            if m:
                name = m.group(1)
                end_line = self._find_brace_end(lines, i)
                # Count methods inside
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
            r'(?:(?:public|private|protected)\s+)?'
            r'(?:(?:static|readonly)\s+)*'
            r'\$(\w+)\s*[;=]'
        )
        _const = re.compile(
            r'(?:(?:public|private|protected)\s+)?'
            r'const\s+(\w+)\s*='
        )

        for i, line in enumerate(lines):
            stripped = line.strip()
            # Skip function definitions
            if re.search(r'\bfunction\b', stripped):
                continue
            m = _property.search(stripped)
            if m:
                variables.append(Variable(
                    name=f"${m.group(1)}", line=i + 1, scope="class",
                ))
                continue
            m = _const.search(stripped)
            if m:
                variables.append(Variable(
                    name=m.group(1), line=i + 1, scope="class",
                ))
        return variables

    # ── exports ───────────────────────────────────────────────────

    def _build_exports(
        self,
        functions: list[Function],
        classes: list[Class],
    ) -> list[Export]:
        """Treat all public classes and public methods as exports."""
        exports: list[Export] = []
        # All top-level classes are public by nature
        for cls in classes:
            exports.append(Export(name=cls.name, line=cls.line, kind="class"))
        # Functions not marked private/protected are public
        for fn in functions:
            exports.append(Export(name=fn.name, line=fn.line, kind="function"))
        return exports

    # ── calls ─────────────────────────────────────────────────────

    def _parse_calls(self, lines: list[str]) -> list[Call]:
        calls: list[Call] = []
        _func_call = re.compile(r'(\w+)\s*\(')
        _method_call = re.compile(r'(?:\$\w+|self|static|parent|\w+)->(\w+)\s*\(')
        _static_call = re.compile(r'(\w+)::(\w+)\s*\(')
        _keywords = {
            "function", "if", "elseif", "else", "for", "foreach", "while",
            "switch", "case", "catch", "class", "interface", "trait", "enum",
            "return", "echo", "print", "new", "throw", "use", "require",
            "require_once", "include", "include_once", "isset", "unset",
            "empty", "list", "array", "match",
        }

        for i, line in enumerate(lines):
            for m in _static_call.finditer(line):
                calls.append(Call(
                    name=f"{m.group(1)}::{m.group(2)}", line=i + 1,
                ))
            for m in _method_call.finditer(line):
                calls.append(Call(name=m.group(1), line=i + 1))
            for m in _func_call.finditer(line):
                name = m.group(1)
                if name not in _keywords:
                    calls.append(Call(name=name, line=i + 1))
        return calls
