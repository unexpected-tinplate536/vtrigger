from __future__ import annotations

import re
from pathlib import Path

from .base import Parser
from ..finding import Call, Class, Export, Function, Import, ParsedFile, Variable


class ShellParser(Parser):
    """Parse Shell/Bash/Zsh files using regex."""

    def supports(self, path: Path) -> bool:
        return path.suffix in (".sh", ".bash", ".zsh")

    def parse_file(self, path: Path) -> ParsedFile:
        source = path.read_text(encoding="utf-8", errors="replace")
        raw_lines = source.count("\n") + (1 if source and not source.endswith("\n") else 0)

        try:
            return self._parse(path, source, raw_lines)
        except Exception:
            return ParsedFile(path=path, raw_lines=raw_lines)

    def _parse(self, path: Path, source: str, raw_lines: int) -> ParsedFile:
        lines = source.splitlines()

        imports = self._parse_imports(lines)
        functions = self._parse_functions(lines)
        variables = self._parse_variables(lines, functions)
        exports = self._build_exports(functions, variables)
        calls = self._parse_calls(lines, functions)

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

    # ── imports ───────────────────────────────────────────────────

    def _parse_imports(self, lines: list[str]) -> list[Import]:
        imports: list[Import] = []
        # source ./file.sh  |  . ./file.sh  |  source /path/to/file
        _source = re.compile(
            r'^(?:source|\.)\s+'
            r'(?:["\'](.*?)["\']|(\S+))'
        )
        for i, line in enumerate(lines):
            stripped = self._strip_comment(line).strip()
            m = _source.match(stripped)
            if m:
                filepath = m.group(1) or m.group(2)
                name = Path(filepath).name
                imports.append(Import(
                    name=name,
                    source=filepath,
                    line=i + 1,
                ))
        return imports

    # ── functions ─────────────────────────────────────────────────

    def _parse_functions(self, lines: list[str]) -> list[Function]:
        functions: list[Function] = []
        # function foo { ... }
        # function foo() { ... }
        # foo() { ... }
        _func = re.compile(
            r'^(?:function\s+(\w[\w-]*)\s*(?:\(\s*\))?\s*\{?'
            r'|(\w[\w-]*)\s*\(\s*\)\s*\{?)'
        )
        for i, line in enumerate(lines):
            stripped = self._strip_comment(line).strip()
            m = _func.match(stripped)
            if m:
                name = m.group(1) or m.group(2)
                end_line = self._find_function_end(lines, i)
                functions.append(Function(
                    name=name,
                    line=i + 1,
                    end_line=end_line + 1,
                    param_count=0,
                    line_count=end_line - i + 1,
                ))
        return functions

    def _find_function_end(self, lines: list[str], start: int) -> int:
        """Find the closing brace of a shell function via brace counting."""
        depth = 0
        for i in range(start, len(lines)):
            line = self._strip_comment(lines[i])
            # Skip strings roughly -- just count braces outside quotes
            in_single = False
            in_double = False
            j = 0
            while j < len(line):
                ch = line[j]
                if ch == '\\' and not in_single:
                    j += 2
                    continue
                if ch == "'" and not in_double:
                    in_single = not in_single
                elif ch == '"' and not in_single:
                    in_double = not in_double
                elif not in_single and not in_double:
                    if ch == '{':
                        depth += 1
                    elif ch == '}':
                        depth -= 1
                        if depth == 0:
                            return i
                j += 1
        return start

    # ── variables ─────────────────────────────────────────────────

    def _parse_variables(
        self, lines: list[str], functions: list[Function]
    ) -> list[Variable]:
        variables: list[Variable] = []
        # export FOO=bar, readonly FOO=bar, declare -x FOO=bar, FOO=bar
        _assign = re.compile(
            r'^(?:export\s+|readonly\s+|declare\s+(?:-\w+\s+)*)?'
            r'([A-Za-z_]\w*)='
        )
        _local = re.compile(
            r'^local\s+([A-Za-z_]\w*)='
        )

        func_ranges = [(f.line, f.end_line) for f in functions]

        for i, line in enumerate(lines):
            stripped = self._strip_comment(line).strip()
            lineno = i + 1

            # Check if inside a function
            in_func = any(start <= lineno <= end for start, end in func_ranges)

            m_local = _local.match(stripped)
            if m_local and in_func:
                variables.append(Variable(
                    name=m_local.group(1), line=lineno, scope="function",
                ))
                continue

            m = _assign.match(stripped)
            if m:
                name = m.group(1)
                # Skip if this looks like a command (e.g., PATH=/usr/bin command)
                if not in_func:
                    variables.append(Variable(
                        name=name, line=lineno, scope="module",
                    ))
        return variables

    # ── exports ───────────────────────────────────────────────────

    def _build_exports(
        self, functions: list[Function], variables: list[Variable]
    ) -> list[Export]:
        exports: list[Export] = []
        # All functions are effectively exported (globally available when sourced)
        for fn in functions:
            exports.append(Export(name=fn.name, line=fn.line, kind="function"))
        # Variables declared with export
        for var in variables:
            if var.scope == "module":
                exports.append(Export(name=var.name, line=var.line, kind="variable"))
        return exports

    # ── calls ─────────────────────────────────────────────────────

    def _parse_calls(
        self, lines: list[str], functions: list[Function]
    ) -> list[Call]:
        calls: list[Call] = []
        func_names = {f.name for f in functions}
        # Direct calls to known functions
        _word = re.compile(r'\b(' + '|'.join(re.escape(n) for n in func_names) + r')\b') if func_names else None
        # Command substitution: $(foo ...) or `foo ...`
        _cmd_sub = re.compile(r'\$\((\w[\w-]*)')
        _backtick = re.compile(r'`(\w[\w-]*)')

        for i, line in enumerate(lines):
            stripped = self._strip_comment(line)
            lineno = i + 1

            # Known function calls
            if _word:
                for m in _word.finditer(stripped):
                    calls.append(Call(name=m.group(1), line=lineno))

            # Command substitution calls
            for m in _cmd_sub.finditer(stripped):
                name = m.group(1)
                if name not in func_names:  # avoid duplicates
                    calls.append(Call(name=name, line=lineno))

            for m in _backtick.finditer(stripped):
                name = m.group(1)
                if name not in func_names:
                    calls.append(Call(name=name, line=lineno))

        return calls

    # ── helpers ───────────────────────────────────────────────────

    def _strip_comment(self, line: str) -> str:
        """Strip # comments but preserve shebang, $#, ${#...} etc."""
        result = []
        in_single = False
        in_double = False
        i = 0
        while i < len(line):
            ch = line[i]
            if ch == '\\' and not in_single and i + 1 < len(line):
                result.append(ch)
                result.append(line[i + 1])
                i += 2
                continue
            if ch == "'" and not in_double:
                in_single = not in_single
                result.append(ch)
            elif ch == '"' and not in_single:
                in_double = not in_double
                result.append(ch)
            elif ch == '#' and not in_single and not in_double:
                # Preserve shebang on first line
                if i == 0 and len(line) > 1 and line[1] == '!':
                    result.append(ch)
                # Preserve $# and ${#
                elif i > 0 and line[i - 1] in ('$', '{'):
                    result.append(ch)
                else:
                    break
            else:
                result.append(ch)
            i += 1
        return ''.join(result)
