from __future__ import annotations

import re
from pathlib import Path

from .base import Parser
from ..finding import Call, Class, Export, Function, Import, ParsedFile, Variable


class NimParser(Parser):
    """Parse Nim files using regex."""

    _CALLABLE_KINDS = ("proc", "func", "method", "template", "macro", "iterator", "converter")

    def supports(self, path: Path) -> bool:
        return path.suffix in (".nim", ".nims")

    def parse_file(self, path: Path) -> ParsedFile:
        source = path.read_text(encoding="utf-8", errors="replace")
        raw_lines = source.count("\n") + (1 if source and not source.endswith("\n") else 0)

        try:
            return self._parse(path, source, raw_lines)
        except Exception:
            return ParsedFile(path=path, raw_lines=raw_lines)

    def _parse(self, path: Path, source: str, raw_lines: int) -> ParsedFile:
        cleaned = self._strip_block_comments(source)
        lines = cleaned.splitlines()
        self._exported_names: dict[str, dict] = {}

        imports = self._parse_imports(lines)
        functions = self._parse_functions(lines)
        classes = self._parse_classes(lines, functions)
        variables = self._parse_variables(lines, functions)
        exports = self._collect_exports()
        calls = self._parse_calls(lines)

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

    # ── imports ───────────────────────────────────────────────────

    def _parse_imports(self, lines: list[str]) -> list[Import]:
        imports: list[Import] = []
        _import = re.compile(
            r'^import\s+([\w/]+)(?:\s+except\s+(.+))?'
        )
        _from_import = re.compile(
            r'^from\s+([\w/]+)\s+import\s+(.+)'
        )
        _include = re.compile(
            r'^include\s+([\w/]+)'
        )

        for i, line in enumerate(lines):
            stripped = self._strip_line_comment(line).strip()
            lineno = i + 1

            m = _from_import.match(stripped)
            if m:
                module = m.group(1)
                names = [n.strip() for n in m.group(2).split(",")]
                for name in names:
                    if name:
                        imports.append(Import(
                            name=name,
                            source=module,
                            line=lineno,
                        ))
                continue

            m = _import.match(stripped)
            if m:
                module = m.group(1)
                short = module.rsplit("/", 1)[-1]
                imports.append(Import(
                    name=short,
                    source=module,
                    line=lineno,
                ))
                continue

            m = _include.match(stripped)
            if m:
                module = m.group(1)
                short = module.rsplit("/", 1)[-1]
                imports.append(Import(
                    name=short,
                    source=module,
                    line=lineno,
                ))
        return imports

    # ── functions ─────────────────────────────────────────────────

    def _parse_functions(self, lines: list[str]) -> list[Function]:
        functions: list[Function] = []
        pattern = re.compile(
            r'^(' + '|'.join(self._CALLABLE_KINDS) + r')\s+'
            r'(\w+)(\*?)\s*'
            r'(?:\[.*?\])?\s*'
            r'\(([^)]*)\)'
        )

        for i, line in enumerate(lines):
            stripped = self._strip_line_comment(line).strip()
            m = pattern.match(stripped)
            if m:
                name = m.group(2)
                exported = m.group(3) == '*'
                params_str = m.group(4).strip()
                param_count = self._count_params(params_str)
                end_line = self._find_block_end(lines, i)
                functions.append(Function(
                    name=name,
                    line=i + 1,
                    end_line=end_line + 1,
                    param_count=param_count,
                    line_count=end_line - i + 1,
                ))
                if exported:
                    self._exported_names[name] = {"line": i + 1, "kind": "function"}
        return functions

    def _count_params(self, params_str: str) -> int:
        if not params_str:
            return 0
        # Nim params: "a, b: int; c: string" or "a, b: int, c: string"
        count = 0
        groups = re.split(r';', params_str)
        for group in groups:
            group = group.strip()
            if not group:
                continue
            if ':' in group:
                names_part = group.split(':', 1)[0]
                names = [n.strip() for n in names_part.split(',') if n.strip()]
                count += len(names)
            else:
                names = [n.strip() for n in group.split(',') if n.strip()]
                count += len(names)
        return count

    def _find_block_end(self, lines: list[str], start: int) -> int:
        """Find end of indentation-based block starting at `start`."""
        if start + 1 >= len(lines):
            return start

        base_indent = self._indent_level(lines[start])
        last_body_line = start
        for i in range(start + 1, len(lines)):
            stripped = self._strip_line_comment(lines[i]).strip()
            if not stripped:
                continue
            indent = self._indent_level(lines[i])
            if indent <= base_indent:
                break
            last_body_line = i
        return last_body_line

    def _indent_level(self, line: str) -> int:
        return len(line) - len(line.lstrip())

    # ── classes (types) ───────────────────────────────────────────

    def _parse_classes(
        self, lines: list[str], functions: list[Function]
    ) -> list[Class]:
        classes: list[Class] = []
        _type_block = re.compile(r'^\s*type\b')
        _type_def = re.compile(
            r'(\w+)(\*?)\s*=\s*(?:ref\s+)?(object|enum|distinct\s+\w+|tuple)'
        )

        in_type_block = False
        type_block_indent = 0

        for i, line in enumerate(lines):
            stripped = self._strip_line_comment(line).strip()
            lineno = i + 1

            # type block start (just "type" on its own line)
            if _type_block.match(line) and stripped == "type":
                in_type_block = True
                type_block_indent = self._indent_level(line)
                continue

            # Single-line: type Foo* = object
            single = re.match(r'^type\s+(.+)', stripped)
            if single:
                m = _type_def.match(single.group(1).strip())
                if m:
                    name = m.group(1)
                    exported = m.group(2) == '*'
                    end_line = self._find_block_end(lines, i)
                    method_count = self._count_type_methods(name, lines)
                    classes.append(Class(
                        name=name, line=lineno, end_line=end_line + 1,
                        method_count=method_count, line_count=end_line - i + 1,
                    ))
                    if exported:
                        self._exported_names[name] = {"line": lineno, "kind": "class"}
                continue

            if in_type_block:
                if stripped and self._indent_level(line) <= type_block_indent:
                    in_type_block = False
                else:
                    m = _type_def.match(stripped)
                    if m:
                        name = m.group(1)
                        exported = m.group(2) == '*'
                        end_line = self._find_block_end(lines, i)
                        method_count = self._count_type_methods(name, lines)
                        classes.append(Class(
                            name=name, line=lineno, end_line=end_line + 1,
                            method_count=method_count, line_count=end_line - i + 1,
                        ))
                        if exported:
                            self._exported_names[name] = {"line": lineno, "kind": "class"}

        return classes

    def _count_type_methods(self, type_name: str, lines: list[str]) -> int:
        """Count procs whose first parameter is of the given type."""
        count = 0
        pattern = re.compile(
            r'^(?:' + '|'.join(self._CALLABLE_KINDS) + r')\s+'
            r'\w+\*?\s*(?:\[.*?\])?\s*\('
            r'\s*\w+\s*:\s*(?:var\s+)?'
            + re.escape(type_name) + r'\b'
        )
        for line in lines:
            stripped = self._strip_line_comment(line).strip()
            if pattern.match(stripped):
                count += 1
        return count

    # ── variables ─────────────────────────────────────────────────

    def _parse_variables(
        self, lines: list[str], functions: list[Function]
    ) -> list[Variable]:
        variables: list[Variable] = []
        _var_decl = re.compile(
            r'^(var|let|const)\s+(\w+)(\*?)\s*(?::\s*[\w\[\], ]+)?(?:\s*=.*)?$'
        )
        _var_block = re.compile(r'^(var|let|const)\s*$')
        _block_item = re.compile(
            r'^\s+(\w+)(\*?)\s*(?::\s*[\w\[\], ]+)?(?:\s*=.*)?$'
        )

        func_ranges = [(f.line, f.end_line) for f in functions]
        in_block = False
        block_indent = 0

        for i, line in enumerate(lines):
            stripped = self._strip_line_comment(line).strip()
            lineno = i + 1
            in_func = any(start <= lineno <= end for start, end in func_ranges)

            if _var_block.match(stripped):
                in_block = True
                block_indent = self._indent_level(line)
                continue

            if in_block:
                if not stripped:
                    continue
                if self._indent_level(line) <= block_indent:
                    in_block = False
                else:
                    m = _block_item.match(line)
                    if m:
                        name = m.group(1)
                        exported = m.group(2) == '*'
                        scope = "function" if in_func else "module"
                        variables.append(Variable(name=name, line=lineno, scope=scope))
                        if exported and not in_func:
                            self._exported_names[name] = {"line": lineno, "kind": "variable"}
                    continue

            m = _var_decl.match(stripped)
            if m:
                name = m.group(2)
                exported = m.group(3) == '*'
                scope = "function" if in_func else "module"
                variables.append(Variable(name=name, line=lineno, scope=scope))
                if exported and not in_func:
                    self._exported_names[name] = {"line": lineno, "kind": "variable"}
        return variables

    # ── exports ───────────────────────────────────────────────────

    def _collect_exports(self) -> list[Export]:
        exports: list[Export] = []
        for name, info in self._exported_names.items():
            exports.append(Export(name=name, line=info["line"], kind=info["kind"]))
        return exports

    # ── calls ─────────────────────────────────────────────────────

    def _parse_calls(self, lines: list[str]) -> list[Call]:
        calls: list[Call] = []
        _call = re.compile(r'(?:(\w+)\.)?(\w+)\s*\(')
        _keywords = {
            "proc", "func", "method", "template", "macro", "iterator",
            "converter", "if", "elif", "else", "when", "while", "for",
            "case", "of", "import", "from", "include", "type", "var",
            "let", "const", "return", "yield", "discard", "block",
            "try", "except", "finally", "raise", "defer",
        }

        for i, line in enumerate(lines):
            stripped = self._strip_line_comment(line)
            for m in _call.finditer(stripped):
                name = m.group(2)
                if name not in _keywords:
                    full_name = f"{m.group(1)}.{name}" if m.group(1) else name
                    calls.append(Call(name=full_name, line=i + 1))
        return calls

    # ── comment stripping ─────────────────────────────────────────

    def _strip_line_comment(self, line: str) -> str:
        """Strip # line comments, preserving strings."""
        result = []
        in_string = False
        string_char = None
        i = 0
        while i < len(line):
            ch = line[i]
            if in_string:
                result.append(ch)
                if ch == '\\':
                    if i + 1 < len(line):
                        result.append(line[i + 1])
                        i += 2
                        continue
                elif ch == string_char:
                    in_string = False
                i += 1
                continue
            if ch in ('"',):
                in_string = True
                string_char = ch
                result.append(ch)
            elif ch == '#':
                break
            else:
                result.append(ch)
            i += 1
        return ''.join(result)

    def _strip_block_comments(self, source: str) -> str:
        """Remove #[ ... ]# block comments (can be nested)."""
        result = []
        i = 0
        while i < len(source):
            if source[i:i+2] == '#[':
                depth = 1
                i += 2
                while i < len(source) and depth > 0:
                    if source[i:i+2] == '#[':
                        depth += 1
                        i += 2
                    elif source[i:i+2] == ']#':
                        depth -= 1
                        i += 2
                    else:
                        # Preserve newlines so line numbers stay correct
                        if source[i] == '\n':
                            result.append('\n')
                        i += 1
            else:
                result.append(source[i])
                i += 1
        return ''.join(result)
