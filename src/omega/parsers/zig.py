from __future__ import annotations

import re
from pathlib import Path

from .base import Parser
from ..finding import Call, Class, Export, Function, Import, ParsedFile, Variable


class ZigParser(Parser):
    """Parse Zig files using regex."""

    def supports(self, path: Path) -> bool:
        return path.suffix == ".zig"

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
            exports = self._build_exports(cleaned)

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

    # -- comment stripping ---------------------------------------------------

    def _strip_comments(self, lines: list[str]) -> list[str]:
        """Strip // line comments, preserving line numbering."""
        result: list[str] = []
        for line in lines:
            # Remove // comments (Zig has no block comments)
            no_comment = re.sub(r'//.*$', '', line)
            result.append(no_comment)
        return result

    # -- imports -------------------------------------------------------------

    def _parse_imports(self, lines: list[str]) -> list[Import]:
        imports: list[Import] = []
        _import = re.compile(
            r'^\s*(?:pub\s+)?const\s+(\w+)\s*=\s*@import\(\s*"([^"]+)"\s*\)'
        )
        _usingnamespace = re.compile(
            r'^\s*(?:pub\s+)?usingnamespace\s+([\w.]+)\s*;'
        )

        for i, line in enumerate(lines):
            m = _import.match(line)
            if m:
                alias = m.group(1)
                source = m.group(2)
                name = source.rstrip(".zig")
                imports.append(Import(
                    name=name, source=source, line=i + 1, alias=alias,
                ))
                continue

            m = _usingnamespace.match(line)
            if m:
                ns = m.group(1)
                imports.append(Import(name=ns, source=ns, line=i + 1))

        return imports

    # -- functions -----------------------------------------------------------

    def _parse_functions(self, lines: list[str]) -> list[Function]:
        functions: list[Function] = []
        _func = re.compile(
            r'^\s*(?:pub\s+)?(?:export\s+)?(?:inline\s+)?(?:comptime\s+)?fn\s+(\w+)\s*\(([^)]*)\)'
        )

        for i, line in enumerate(lines):
            m = _func.match(line)
            if m:
                name = m.group(1)
                params_str = m.group(2)
                param_count = self._count_params(params_str)
                end_line = self._find_closing_brace(lines, i)
                functions.append(Function(
                    name=name,
                    line=i + 1,
                    end_line=end_line + 1,
                    param_count=param_count,
                    line_count=end_line - i + 1,
                ))
        return functions

    def _count_params(self, params_str: str) -> int:
        stripped = params_str.strip()
        if not stripped:
            return 0
        params = [p.strip() for p in stripped.split(",") if p.strip()]
        return len(params)

    def _find_closing_brace(self, lines: list[str], start: int) -> int:
        """Find matching closing brace using brace counting from `start`."""
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

    # -- classes (struct, enum, union) ---------------------------------------

    def _parse_classes(self, lines: list[str]) -> list[Class]:
        classes: list[Class] = []
        _struct = re.compile(
            r'^\s*(?:pub\s+)?const\s+(\w+)\s*=\s*(?:packed\s+|extern\s+)?(?:struct|enum|union)'
        )
        _method = re.compile(r'^\s*(?:pub\s+)?(?:inline\s+)?fn\s+\w+')

        for i, line in enumerate(lines):
            m = _struct.match(line)
            if m:
                name = m.group(1)
                end_line = self._find_closing_brace(lines, i)
                method_count = 0
                for j in range(i + 1, end_line):
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

    # -- variables -----------------------------------------------------------

    def _parse_variables(self, lines: list[str]) -> list[Variable]:
        variables: list[Variable] = []
        _var = re.compile(r'^\s*(?:pub\s+)?(const|var)\s+(\w+)\s*')
        # Skip lines that are imports or struct/enum/union definitions
        _skip = re.compile(
            r'=\s*@import\(|=\s*(?:packed\s+|extern\s+)?(?:struct|enum|union)\b'
        )
        _func_start = re.compile(
            r'^\s*(?:pub\s+)?(?:export\s+)?(?:inline\s+)?(?:comptime\s+)?fn\s+'
        )

        # Track whether we are inside a function body
        in_func_depth = 0
        func_brace_depth = 0

        for i, line in enumerate(lines):
            stripped = line.strip()
            if not stripped:
                continue

            # Track function scope via brace depth
            if _func_start.match(line) and in_func_depth == 0:
                in_func_depth = 1
                func_brace_depth = 0
                for ch in line:
                    if ch == "{":
                        func_brace_depth += 1
                    elif ch == "}":
                        func_brace_depth -= 1
                if func_brace_depth <= 0 and "{" in line:
                    in_func_depth = 0
                continue

            if in_func_depth > 0:
                for ch in line:
                    if ch == "{":
                        func_brace_depth += 1
                    elif ch == "}":
                        func_brace_depth -= 1
                if func_brace_depth <= 0:
                    in_func_depth = 0
                continue

            m = _var.match(line)
            if m and not _skip.search(line):
                name = m.group(2)
                variables.append(Variable(name=name, line=i + 1, scope="module"))

        return variables

    # -- exports -------------------------------------------------------------

    def _build_exports(self, lines: list[str]) -> list[Export]:
        """Detect pub-marked declarations as exports."""
        exports: list[Export] = []
        _pub_fn = re.compile(
            r'^\s*pub\s+(?:export\s+)?(?:inline\s+)?(?:comptime\s+)?fn\s+(\w+)'
        )
        _pub_const_struct = re.compile(
            r'^\s*pub\s+const\s+(\w+)\s*=\s*(?:packed\s+|extern\s+)?(?:struct|enum|union)'
        )
        _pub_var = re.compile(r'^\s*pub\s+(?:const|var)\s+(\w+)')

        seen: set[str] = set()
        for i, line in enumerate(lines):
            m = _pub_fn.match(line)
            if m:
                name = m.group(1)
                if name not in seen:
                    exports.append(Export(name=name, line=i + 1, kind="function"))
                    seen.add(name)
                continue
            m = _pub_const_struct.match(line)
            if m:
                name = m.group(1)
                if name not in seen:
                    exports.append(Export(name=name, line=i + 1, kind="class"))
                    seen.add(name)
                continue
            m = _pub_var.match(line)
            if m:
                name = m.group(1)
                if name not in seen:
                    exports.append(Export(name=name, line=i + 1, kind="variable"))
                    seen.add(name)
        return exports

    # -- calls ---------------------------------------------------------------

    def _parse_calls(self, lines: list[str]) -> list[Call]:
        calls: list[Call] = []
        _call = re.compile(r'(?<![.@\w])([\w][\w.]*)\s*\(')
        _method_chain = re.compile(r'\)\s*\.\s*(\w+)\s*\(')
        _builtin = re.compile(r'(@\w+)\s*\(')
        _func_decl = re.compile(
            r'^\s*(?:pub\s+)?(?:export\s+)?(?:inline\s+)?(?:comptime\s+)?fn\s+(\w+)'
        )
        _keywords = {
            "fn", "if", "else", "while", "for", "switch", "return",
            "const", "var", "pub", "struct", "enum", "union",
            "try", "catch", "comptime", "inline", "export",
            "test", "defer", "errdefer", "unreachable",
        }

        for i, line in enumerate(lines):
            # Skip function declarations (the name in `fn foo(` is not a call)
            decl_match = _func_decl.match(line)
            decl_name = decl_match.group(1) if decl_match else None

            for m in _builtin.finditer(line):
                name = m.group(1)
                calls.append(Call(name=name, line=i + 1))
            for m in _call.finditer(line):
                name = m.group(1)
                base = name.split(".")[-1] if "." in name else name
                if base not in _keywords and name != decl_name:
                    calls.append(Call(name=name, line=i + 1))
            for m in _method_chain.finditer(line):
                name = m.group(1)
                if name not in _keywords:
                    calls.append(Call(name=name, line=i + 1))
        return calls
