from __future__ import annotations

import re
from pathlib import Path

from .base import Parser
from ..finding import Call, Class, Export, Function, Import, ParsedFile, Variable


class LuaParser(Parser):
    """Parse Lua files using regex."""

    def supports(self, path: Path) -> bool:
        return path.suffix == ".lua"

    def parse_file(self, path: Path) -> ParsedFile:
        source = path.read_text(encoding="utf-8", errors="replace")
        raw_lines = source.count("\n") + (1 if source and not source.endswith("\n") else 0)

        try:
            cleaned = self._strip_comments(source)
            lines = cleaned.splitlines()

            imports = self._parse_imports(lines)
            functions = self._parse_functions(lines)
            variables = self._parse_variables(lines)
            calls = self._parse_calls(lines)
            exports = self._build_exports(lines, functions, variables)

            return ParsedFile(
                path=path,
                imports=imports,
                exports=exports,
                functions=functions,
                classes=[],
                variables=variables,
                calls=calls,
                raw_lines=raw_lines,
            )
        except Exception:
            return ParsedFile(path=path, raw_lines=raw_lines)

    # ── comment stripping ─────────────────────────────────────────

    def _strip_comments(self, source: str) -> str:
        # Remove block comments --[[ ... ]] (with optional = signs: --[==[ ... ]==])
        source = re.sub(r'--\[(=*)\[.*?\]\1\]', '', source, flags=re.DOTALL)
        # Remove line comments -- ...
        source = re.sub(r'--[^\[\n][^\n]*', '', source)
        # Remove line comments that are just --\n or -- at end of line
        source = re.sub(r'--$', '', source, flags=re.MULTILINE)
        return source

    # ── imports ───────────────────────────────────────────────────

    def _parse_imports(self, lines: list[str]) -> list[Import]:
        imports: list[Import] = []
        # local M = require("module") or require "module" or require('module')
        _require = re.compile(
            r'(?:local\s+(\w+)\s*=\s*)?'
            r'require\s*[\("]\s*(["\']?)([^"\')\s]+)\2\s*["\')]?'
        )

        for i, line in enumerate(lines):
            for m in _require.finditer(line.strip()):
                alias = m.group(1)
                module = m.group(3)
                short = module.rsplit(".", 1)[-1]
                imports.append(Import(
                    name=short,
                    source=module,
                    line=i + 1,
                    alias=alias,
                ))
        return imports

    # ── functions ─────────────────────────────────────────────────

    def _parse_functions(self, lines: list[str]) -> list[Function]:
        functions: list[Function] = []

        # function foo(a, b)
        _func_named = re.compile(
            r'^(?:local\s+)?function\s+([\w.:]+)\s*\(([^)]*)\)'
        )
        # foo = function(a, b)
        _func_assign = re.compile(
            r'^(?:local\s+)?([\w.]+)\s*=\s*function\s*\(([^)]*)\)'
        )

        for i, line in enumerate(lines):
            stripped = line.strip()
            if not stripped:
                continue

            m = _func_named.match(stripped)
            if m:
                name = m.group(1)
                params = m.group(2)
                param_count = self._count_params(params)
                end_line = self._find_end(lines, i)
                functions.append(Function(
                    name=name,
                    line=i + 1,
                    end_line=end_line + 1,
                    param_count=param_count,
                    line_count=end_line - i + 1,
                ))
                continue

            m = _func_assign.match(stripped)
            if m:
                name = m.group(1)
                params = m.group(2)
                param_count = self._count_params(params)
                end_line = self._find_end(lines, i)
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

    def _find_end(self, lines: list[str], start: int) -> int:
        """Find the matching `end` keyword for a block starting at `start`.
        Tracks nested blocks (function, if, for, while, do, repeat)."""
        # Keywords that open a block (need a matching `end`)
        _open = re.compile(
            r'\b(?:function|if|for|while|do)\b'
        )
        _repeat = re.compile(r'\brepeat\b')
        _until = re.compile(r'\buntil\b')
        _end = re.compile(r'\bend\b')

        depth = 0
        repeat_depth = 0

        for i in range(start, len(lines)):
            line = lines[i].strip()

            # Count block openers
            for _ in _open.finditer(line):
                depth += 1
            for _ in _repeat.finditer(line):
                repeat_depth += 1

            # Count block closers
            for _ in _until.finditer(line):
                repeat_depth -= 1
            for _ in _end.finditer(line):
                depth -= 1
                if depth == 0:
                    return i

        return start

    # ── variables ─────────────────────────────────────────────────

    def _parse_variables(self, lines: list[str]) -> list[Variable]:
        variables: list[Variable] = []
        # local x = ...
        _local = re.compile(r'^local\s+(\w+)\s*=')
        # M.x = ... (module table assignment)
        _table = re.compile(r'^(\w+\.\w+)\s*=\s*(?!function\b)')
        # Global assignment x = ... (not inside a function context, simple heuristic)
        _global = re.compile(r'^(\w+)\s*=\s*(?!function\b)')

        _keywords = {
            "if", "else", "elseif", "then", "end", "for", "while", "do",
            "repeat", "until", "return", "local", "function", "true",
            "false", "nil", "and", "or", "not", "in", "break", "goto",
        }

        # Track depth to detect top-level assignments
        _open = re.compile(r'\b(?:function|if|for|while|do)\b')
        _repeat = re.compile(r'\brepeat\b')
        _until = re.compile(r'\buntil\b')
        _end_kw = re.compile(r'\bend\b')

        depth = 0

        for i, line in enumerate(lines):
            stripped = line.strip()
            if not stripped:
                continue

            # Only capture top-level variables
            if depth == 0:
                # Skip lines that are function definitions (handled by _parse_functions)
                if re.match(r'^(?:local\s+)?function\b', stripped):
                    pass  # fall through to depth tracking
                elif re.match(r'^(?:local\s+)?\w+\s*=\s*function\b', stripped):
                    pass  # fall through to depth tracking
                else:
                    m = _local.match(stripped)
                    if m:
                        name = m.group(1)
                        if name not in _keywords:
                            variables.append(Variable(name=name, line=i + 1, scope="module"))
                            # Update depth and continue
                            depth += len(_open.findall(stripped)) + len(_repeat.findall(stripped))
                            depth -= len(_end_kw.findall(stripped)) + len(_until.findall(stripped))
                            continue

                    m = _table.match(stripped)
                    if m:
                        name = m.group(1)
                        variables.append(Variable(name=name, line=i + 1, scope="module"))
                        depth += len(_open.findall(stripped)) + len(_repeat.findall(stripped))
                        depth -= len(_end_kw.findall(stripped)) + len(_until.findall(stripped))
                        continue

                    m = _global.match(stripped)
                    if m:
                        name = m.group(1)
                        if name not in _keywords:
                            variables.append(Variable(name=name, line=i + 1, scope="module"))

            # Track depth
            depth += len(_open.findall(stripped)) + len(_repeat.findall(stripped))
            depth -= len(_end_kw.findall(stripped)) + len(_until.findall(stripped))
            if depth < 0:
                depth = 0

        return variables

    # ── calls ─────────────────────────────────────────────────────

    def _parse_calls(self, lines: list[str]) -> list[Call]:
        calls: list[Call] = []
        _call = re.compile(r'([\w.:]+)\s*[(\"]')
        _keywords = {
            "function", "if", "elseif", "for", "while", "return",
            "local", "and", "or", "not", "end", "do", "then",
            "else", "repeat", "until", "in", "true", "false", "nil",
        }

        for i, line in enumerate(lines):
            for m in _call.finditer(line):
                name = m.group(1)
                # Skip keywords and plain numbers
                if name not in _keywords and not name.isdigit():
                    calls.append(Call(name=name, line=i + 1))
        return calls

    # ── exports ───────────────────────────────────────────────────

    def _build_exports(
        self,
        lines: list[str],
        functions: list[Function],
        variables: list[Variable],
    ) -> list[Export]:
        """Lua modules typically `return M` at end of file.
        Functions/variables on the M table are the exports."""
        exports: list[Export] = []

        # Find the module table name from `return <name>` at the end of the file
        module_name: str | None = None
        _return = re.compile(r'^return\s+(\w+)\s*$')
        for line in reversed(lines):
            stripped = line.strip()
            if not stripped:
                continue
            m = _return.match(stripped)
            if m:
                module_name = m.group(1)
            break  # Only check the last non-empty line

        if module_name:
            prefix = f"{module_name}."
            colon_prefix = f"{module_name}:"
            for fn in functions:
                if fn.name.startswith(prefix) or fn.name.startswith(colon_prefix):
                    short = fn.name.split(".", 1)[-1] if "." in fn.name else fn.name.split(":", 1)[-1]
                    exports.append(Export(name=short, line=fn.line, kind="function"))
            for var in variables:
                if var.name.startswith(prefix):
                    short = var.name.split(".", 1)[-1]
                    exports.append(Export(name=short, line=var.line, kind="variable"))
        else:
            # No module return pattern; global functions are effectively exports
            for fn in functions:
                if "." not in fn.name and ":" not in fn.name and not fn.name.startswith("_"):
                    # Skip local functions (they start with local in the source)
                    # We can't easily check here, so export all top-level named functions
                    exports.append(Export(name=fn.name, line=fn.line, kind="function"))

        return exports
