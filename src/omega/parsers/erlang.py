from __future__ import annotations

import re
from pathlib import Path

from .base import Parser
from ..finding import Call, Class, Export, Function, Import, ParsedFile, Variable


class ErlangParser(Parser):
    """Parse Erlang files using regex-based parsing."""

    def supports(self, path: Path) -> bool:
        return path.suffix in (".erl", ".hrl")

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
        variables = self._parse_variables(lines)
        calls = self._parse_calls(lines)
        exports = self._parse_exports(lines)

        return ParsedFile(
            path=path,
            imports=imports,
            exports=exports,
            functions=functions,
            classes=[],  # Erlang has no classes
            variables=variables,
            calls=calls,
            raw_lines=raw_lines,
        )

    def _strip_comments(self, source: str) -> str:
        # Erlang only has % line comments (no block comments)
        return re.sub(r'%[^\n]*', '', source)

    # ── imports ───────────────────────────────────────────────────

    def _parse_imports(self, lines: list[str]) -> list[Import]:
        imports: list[Import] = []

        # -module(foo).
        _module = re.compile(r'^-module\((\w+)\)\.')
        # -include("file.hrl").
        _include = re.compile(r'^-include\("([^"]+)"\)\.')
        # -include_lib("app/include/file.hrl").
        _include_lib = re.compile(r'^-include_lib\("([^"]+)"\)\.')
        # -import(module, [func/arity, ...]).
        _import = re.compile(r'^-import\((\w+),\s*\[([^\]]*)\]\)\.')

        for i, line in enumerate(lines):
            stripped = line.strip()

            m = _module.match(stripped)
            if m:
                imports.append(Import(
                    name=m.group(1),
                    source=None,
                    line=i + 1,
                ))
                continue

            m = _include.match(stripped)
            if m:
                path_str = m.group(1)
                name = path_str.split("/")[-1] if "/" in path_str else path_str
                imports.append(Import(
                    name=name,
                    source=path_str,
                    line=i + 1,
                ))
                continue

            m = _include_lib.match(stripped)
            if m:
                path_str = m.group(1)
                name = path_str.split("/")[-1] if "/" in path_str else path_str
                imports.append(Import(
                    name=name,
                    source=path_str,
                    line=i + 1,
                ))
                continue

            m = _import.match(stripped)
            if m:
                module = m.group(1)
                func_list = m.group(2)
                for func_match in re.finditer(r'(\w+)/(\d+)', func_list):
                    imports.append(Import(
                        name=func_match.group(1),
                        source=module,
                        line=i + 1,
                    ))

        return imports

    # ── functions ─────────────────────────────────────────────────

    def _parse_functions(self, lines: list[str]) -> list[Function]:
        functions: list[Function] = []
        seen: dict[str, int] = {}  # name -> index in functions list

        # Erlang function clause: name(Arg1, Arg2) ->
        # Function names are lowercase atoms
        _func = re.compile(r"^(\w+)\(([^)]*)\)\s*(?:when\s+.*)?->")
        # Also match name() -> (no args)
        _func_no_args = re.compile(r"^(\w+)\(\)\s*(?:when\s+.*)?->")

        # Join all lines to find period-terminated function ends
        full_text = "\n".join(lines)

        for i, line in enumerate(lines):
            stripped = line.strip()

            # Skip directives
            if stripped.startswith("-"):
                continue

            m = _func.match(stripped)
            if not m:
                m = _func_no_args.match(stripped)

            if m:
                name = m.group(1)
                # Skip Erlang keywords/directives that look like functions
                if name in ("if", "case", "receive", "try", "begin", "fun", "query"):
                    continue

                params_str = m.group(2) if m.lastindex >= 2 else ""
                param_count = self._count_params(params_str)

                # Find the end of this clause: look for a period at end of line
                end_line = self._find_clause_end(lines, i)

                if name in seen:
                    # Multiple clauses of same function: extend end_line
                    idx = seen[name]
                    existing = functions[idx]
                    functions[idx] = Function(
                        name=existing.name,
                        line=existing.line,
                        end_line=end_line + 1,
                        param_count=existing.param_count,
                        line_count=end_line - existing.line + 2,
                    )
                else:
                    seen[name] = len(functions)
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
        # Handle nested structures in params
        depth = 0
        count = 1
        for ch in params_str:
            if ch in ("(", "[", "{", "<"):
                depth += 1
            elif ch in (")", "]", "}", ">"):
                depth -= 1
            elif ch == "," and depth == 0:
                count += 1
        return count

    def _find_clause_end(self, lines: list[str], start: int) -> int:
        """Find the end of an Erlang clause (terminated by . or ;)."""
        for i in range(start, len(lines)):
            stripped = lines[i].rstrip()
            if stripped.endswith(".") or stripped.endswith(";"):
                return i
        return start

    # ── variables (macros) ────────────────────────────────────────

    def _parse_variables(self, lines: list[str]) -> list[Variable]:
        variables: list[Variable] = []
        # -define(MACRO, value).
        _define = re.compile(r'^-define\((\w+)')

        for i, line in enumerate(lines):
            stripped = line.strip()
            m = _define.match(stripped)
            if m:
                variables.append(Variable(
                    name=m.group(1),
                    line=i + 1,
                    scope="module",
                ))

        return variables

    # ── exports ───────────────────────────────────────────────────

    def _parse_exports(self, lines: list[str]) -> list[Export]:
        exports: list[Export] = []
        # -export([foo/2, bar/1]).
        _export = re.compile(r'^-export\(\[([^\]]*)\]\)\.')
        # -compile(export_all).
        _export_all = re.compile(r'^-compile\(export_all\)\.')

        export_all = False

        for i, line in enumerate(lines):
            stripped = line.strip()

            if _export_all.match(stripped):
                export_all = True
                continue

            m = _export.match(stripped)
            if m:
                func_list = m.group(1)
                for func_match in re.finditer(r'(\w+)/(\d+)', func_list):
                    exports.append(Export(
                        name=func_match.group(1),
                        line=i + 1,
                        kind="function",
                    ))

        # If export_all, we would need to add all parsed functions
        # but we don't have them yet at this point. Handle in _parse.
        if export_all:
            # Mark with a sentinel; will be resolved in _parse
            exports.append(Export(name="__export_all__", line=0, kind="function"))

        return exports

    # ── calls ─────────────────────────────────────────────────────

    def _parse_calls(self, lines: list[str]) -> list[Call]:
        calls: list[Call] = []
        # Local call: foo(X)
        _local = re.compile(r'(\w+)\s*\(')
        # Remote call: module:function(X)
        _remote = re.compile(r'(\w+):(\w+)\s*\(')
        _keywords = {
            "if", "case", "receive", "try", "begin", "fun", "end",
            "when", "of", "catch", "after", "query", "not", "and",
            "or", "xor", "band", "bor", "bxor", "bsl", "bsr",
            "div", "rem", "spec", "type", "export", "module",
            "import", "include", "include_lib", "define", "ifdef",
            "ifndef", "else", "endif", "undef", "record", "behaviour",
            "behavior", "callback", "compile",
        }

        for i, line in enumerate(lines):
            stripped = line.strip()
            # Skip directives
            if stripped.startswith("-"):
                continue

            # Remote calls first (more specific)
            for m in _remote.finditer(line):
                module = m.group(1)
                func = m.group(2)
                calls.append(Call(
                    name=f"{module}:{func}",
                    line=i + 1,
                ))

            # Local calls
            for m in _local.finditer(line):
                name = m.group(1)
                # Skip if it's part of a remote call (already captured)
                pos = m.start()
                if pos > 0 and line[pos - 1] == ":":
                    continue
                if name not in _keywords:
                    calls.append(Call(name=name, line=i + 1))

        return calls
