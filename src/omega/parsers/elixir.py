from __future__ import annotations

import re
from pathlib import Path

from .base import Parser
from ..finding import Call, Class, Export, Function, Import, ParsedFile, Variable


class ElixirParser(Parser):
    """Parse Elixir files using regex."""

    def supports(self, path: Path) -> bool:
        return path.suffix in (".ex", ".exs")

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
            exports = self._build_exports(cleaned, functions, classes)

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
        """Strip single-line # comments, preserving line numbering."""
        result: list[str] = []
        for line in lines:
            # Remove # comments, but not #{} interpolation inside strings
            no_comment = re.sub(r'#(?!\{).*$', '', line)
            result.append(no_comment)
        return result

    # -- imports -------------------------------------------------------------

    def _parse_imports(self, lines: list[str]) -> list[Import]:
        imports: list[Import] = []
        _import = re.compile(r'^\s*import\s+([\w.]+)')
        _alias_single = re.compile(r'^\s*alias\s+([\w.]+)\s*$')
        _alias_multi = re.compile(r'^\s*alias\s+([\w.]+)\.\{([^}]+)\}')
        _require = re.compile(r'^\s*require\s+([\w.]+)')
        _use = re.compile(r'^\s*use\s+([\w.]+)')

        for i, line in enumerate(lines):
            stripped = line.strip()

            m = _import.match(stripped)
            if m:
                mod = m.group(1)
                imports.append(Import(name=mod, source=mod, line=i + 1))
                continue

            m = _alias_multi.match(stripped)
            if m:
                base = m.group(1)
                names = [n.strip() for n in m.group(2).split(",") if n.strip()]
                for name in names:
                    full = f"{base}.{name}"
                    imports.append(Import(
                        name=full, source=base, line=i + 1, alias=name,
                    ))
                continue

            m = _alias_single.match(stripped)
            if m:
                full = m.group(1)
                short = full.rsplit(".", 1)[-1]
                imports.append(Import(
                    name=full, source=full, line=i + 1, alias=short,
                ))
                continue

            m = _require.match(stripped)
            if m:
                mod = m.group(1)
                imports.append(Import(name=mod, source=mod, line=i + 1))
                continue

            m = _use.match(stripped)
            if m:
                mod = m.group(1)
                imports.append(Import(name=mod, source=mod, line=i + 1))

        return imports

    # -- functions -----------------------------------------------------------

    def _parse_functions(self, lines: list[str]) -> list[Function]:
        functions: list[Function] = []
        _func = re.compile(
            r'^\s*(?:defmacrop|defmacro|defp|def)\s+(\w+[?!]?)\s*(\([^)]*\))?'
        )
        _oneliner = re.compile(r',\s*do:')

        for i, line in enumerate(lines):
            m = _func.match(line)
            if m:
                name = m.group(1)
                params = m.group(2)
                param_count = self._count_params(params)

                # One-liner: def foo(x), do: ...
                if _oneliner.search(line):
                    end_line = i
                else:
                    end_line = self._find_end_keyword(lines, i)

                functions.append(Function(
                    name=name,
                    line=i + 1,
                    end_line=end_line + 1,
                    param_count=param_count,
                    line_count=end_line - i + 1,
                ))
        return functions

    def _count_params(self, params_str: str | None) -> int:
        if not params_str:
            return 0
        inner = params_str.strip("()")
        if not inner.strip():
            return 0
        return len([p for p in inner.split(",") if p.strip()])

    def _find_end_keyword(self, lines: list[str], start: int) -> int:
        """Find matching `end` for a block starting at `start`."""
        depth = 0
        _opener = re.compile(
            r'^\s*(?:defmodule|defprotocol|defimpl|defmacrop|defmacro|defp|def|if|unless|case|cond|with|for|try|receive|fn)\b'
        )
        _oneliner_do = re.compile(r',\s*do:')
        _do = re.compile(r'\bdo\s*$')
        _end = re.compile(r'^\s*end\b')

        for i in range(start, len(lines)):
            stripped = lines[i].strip()
            if not stripped:
                continue

            # One-liner `def foo(x), do: expr` has no matching `end`
            if _opener.match(lines[i]) and _oneliner_do.search(lines[i]):
                continue

            if _opener.match(lines[i]):
                depth += 1
            elif _do.search(lines[i]) and not _opener.match(lines[i]):
                depth += 1

            if _end.match(lines[i]):
                depth -= 1
                if depth == 0:
                    return i
        return start

    # -- classes (defmodule) -------------------------------------------------

    def _parse_classes(self, lines: list[str]) -> list[Class]:
        classes: list[Class] = []
        _module = re.compile(r'^\s*defmodule\s+([\w.]+)')
        _method = re.compile(r'^\s*(?:defmacrop|defmacro|defp|def)\s+\w+')

        for i, line in enumerate(lines):
            m = _module.match(line)
            if m:
                name = m.group(1)
                end_line = self._find_end_keyword(lines, i)
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

    # -- variables (module attributes) ---------------------------------------

    def _parse_variables(self, lines: list[str]) -> list[Variable]:
        variables: list[Variable] = []
        _attr = re.compile(r'^\s*@(\w+)\s+')

        for i, line in enumerate(lines):
            m = _attr.match(line)
            if m:
                name = f"@{m.group(1)}"
                variables.append(Variable(name=name, line=i + 1, scope="module"))
        return variables

    # -- exports -------------------------------------------------------------

    def _build_exports(
        self,
        lines: list[str],
        functions: list[Function],
        classes: list[Class],
    ) -> list[Export]:
        exports: list[Export] = []
        _private = re.compile(r'^\s*(?:defp|defmacrop)\s+')

        for fn in functions:
            # line is 1-indexed, lines list is 0-indexed
            src_line = lines[fn.line - 1] if fn.line - 1 < len(lines) else ""
            if not _private.match(src_line):
                exports.append(Export(name=fn.name, line=fn.line, kind="function"))

        for cls in classes:
            exports.append(Export(name=cls.name, line=cls.line, kind="class"))
        return exports

    # -- calls ---------------------------------------------------------------

    def _parse_calls(self, lines: list[str]) -> list[Call]:
        calls: list[Call] = []
        _call = re.compile(r'([\w.]+)\s*\(')
        _pipe_call = re.compile(r'\|>\s*([\w.]+)')
        _keywords = {
            "def", "defp", "defmodule", "defmacro", "defmacrop",
            "defprotocol", "defimpl", "defstruct", "defguard", "defguardp",
            "defdelegate", "defexception", "defoverridable",
            "if", "unless", "case", "cond", "with", "for", "try",
            "receive", "fn", "quote", "unquote", "raise",
            "import", "alias", "require", "use",
        }

        for i, line in enumerate(lines):
            for m in _call.finditer(line):
                name = m.group(1)
                base = name.split(".")[-1] if "." in name else name
                if base not in _keywords:
                    calls.append(Call(name=name, line=i + 1))
            for m in _pipe_call.finditer(line):
                name = m.group(1)
                base = name.split(".")[-1] if "." in name else name
                if base not in _keywords:
                    calls.append(Call(name=name, line=i + 1))
        return calls
