from __future__ import annotations

import re
from pathlib import Path

from .base import Parser
from ..finding import Call, Class, Export, Function, Import, ParsedFile, Variable


class RubyParser(Parser):
    """Parse Ruby files using regex."""

    def supports(self, path: Path) -> bool:
        return path.suffix == ".rb"

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
            exports = self._build_exports(functions, classes, cleaned)

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
        """Strip single-line # comments and =begin/=end block comments.

        Returns lines with comments replaced by empty strings, preserving
        line numbering.
        """
        result: list[str] = []
        in_block = False
        for line in lines:
            stripped = line.strip()
            if in_block:
                if stripped == "=end":
                    in_block = False
                result.append("")
                continue
            if stripped.startswith("=begin"):
                in_block = True
                result.append("")
                continue
            # Remove inline comments (but not inside strings -- rough heuristic)
            no_comment = re.sub(r'#[^{].*$', '', line)
            result.append(no_comment)
        return result

    # ── imports ───────────────────────────────────────────────────

    def _parse_imports(self, lines: list[str]) -> list[Import]:
        imports: list[Import] = []
        _require = re.compile(r"""require(?:_relative)?\s+['"]([^'"]+)['"]""")
        _include_extend = re.compile(r'(?:include|extend)\s+(\w[\w:]*)')

        for i, line in enumerate(lines):
            stripped = line.strip()
            m = _require.search(stripped)
            if m:
                source = m.group(1)
                name = source.rsplit("/", 1)[-1]
                is_relative = "require_relative" in stripped
                imports.append(Import(
                    name=name,
                    source=source,
                    line=i + 1,
                ))
                continue
            m = _include_extend.match(stripped)
            if m:
                mod_name = m.group(1)
                imports.append(Import(
                    name=mod_name,
                    source=None,
                    line=i + 1,
                ))
        return imports

    # ── functions ─────────────────────────────────────────────────

    def _parse_functions(self, lines: list[str]) -> list[Function]:
        functions: list[Function] = []
        _func = re.compile(r'^(\s*)def\s+(self\.)?(\w+[?!=]?)\s*(\([^)]*\))?')

        for i, line in enumerate(lines):
            m = _func.match(line)
            if m:
                indent, is_class_method, name, params = m.groups()
                if is_class_method:
                    name = f"self.{name}"
                param_count = self._count_params(params)
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
        """Find matching `end` for a def/class/module starting at `start`."""
        depth = 0
        _opener = re.compile(
            r'^\s*(?:def|class|module|if|unless|while|until|for|case|begin|do)\b'
        )
        _end = re.compile(r'^\s*end\b')
        # Also count do blocks on same line
        _inline_do = re.compile(r'\bdo\s*(\|[^|]*\|)?\s*$')

        for i in range(start, len(lines)):
            stripped = lines[i].strip()
            if not stripped:
                continue
            if _opener.match(lines[i]):
                depth += 1
            elif _inline_do.search(lines[i]) and not _opener.match(lines[i]):
                depth += 1
            if _end.match(lines[i]):
                depth -= 1
                if depth == 0:
                    return i
        return start

    # ── classes ───────────────────────────────────────────────────

    def _parse_classes(self, lines: list[str]) -> list[Class]:
        classes: list[Class] = []
        _class = re.compile(r'^\s*(?:class|module)\s+(\w+)')
        _method = re.compile(r'^\s*def\s+')

        for i, line in enumerate(lines):
            m = _class.match(line)
            if m:
                name = m.group(1)
                end_line = self._find_end_keyword(lines, i)
                # Count methods inside
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

    # ── variables ─────────────────────────────────────────────────

    def _parse_variables(self, lines: list[str]) -> list[Variable]:
        variables: list[Variable] = []
        _constant = re.compile(r'^\s*([A-Z][A-Z0-9_]*)\s*=')
        _class_var = re.compile(r'^\s*(@@\w+)\s*=')
        _instance_var = re.compile(r'^\s*(@\w+)\s*=')

        # Track nesting to determine scope
        for i, line in enumerate(lines):
            stripped = line.strip()
            m = _constant.match(stripped)
            if m:
                variables.append(Variable(
                    name=m.group(1), line=i + 1, scope="module",
                ))
                continue
            m = _class_var.match(stripped)
            if m:
                variables.append(Variable(
                    name=m.group(1), line=i + 1, scope="class",
                ))
                continue
            m = _instance_var.match(stripped)
            if m:
                # Only class-level instance variables (not inside methods)
                if not self._inside_method(lines, i):
                    variables.append(Variable(
                        name=m.group(1), line=i + 1, scope="class",
                    ))
        return variables

    def _inside_method(self, lines: list[str], line_idx: int) -> bool:
        """Rough check: is this line inside a def...end block?"""
        depth = 0
        _def = re.compile(r'^\s*def\b')
        _end = re.compile(r'^\s*end\b')
        _opener = re.compile(
            r'^\s*(?:class|module|if|unless|while|until|for|case|begin|do)\b'
        )
        for i in range(line_idx):
            if _def.match(lines[i]):
                depth += 1
            elif _opener.match(lines[i]):
                pass  # don't track non-def openers for this heuristic
            elif _end.match(lines[i]):
                if depth > 0:
                    depth -= 1
        return depth > 0

    # ── exports ───────────────────────────────────────────────────

    def _build_exports(
        self,
        functions: list[Function],
        classes: list[Class],
        lines: list[str],
    ) -> list[Export]:
        """Treat all non-private methods and classes as exports."""
        exports: list[Export] = []
        private_methods = self._find_private_methods(lines)

        for fn in functions:
            name = fn.name.replace("self.", "")
            if name not in private_methods:
                exports.append(Export(name=fn.name, line=fn.line, kind="function"))
        for cls in classes:
            exports.append(Export(name=cls.name, line=cls.line, kind="class"))
        return exports

    def _find_private_methods(self, lines: list[str]) -> set[str]:
        """Find methods declared after `private` keyword."""
        private_methods: set[str] = set()
        _private = re.compile(r'^\s*private\s*$')
        _protected = re.compile(r'^\s*protected\s*$')
        _public = re.compile(r'^\s*public\s*$')
        _method = re.compile(r'^\s*def\s+(?:self\.)?(\w+[?!=]?)')
        _end = re.compile(r'^\s*end\s*$')

        # Track visibility within each class/module scope
        in_private = False
        class_depth = 0
        _class_open = re.compile(r'^\s*(?:class|module)\b')

        for line in lines:
            stripped = line.strip()
            if _class_open.match(line):
                class_depth += 1
                in_private = False
            elif _end.match(line):
                if class_depth > 0:
                    class_depth -= 1
                    in_private = False
            elif _private.match(line):
                in_private = True
            elif _public.match(line) or _protected.match(line):
                in_private = False
            elif in_private:
                m = _method.match(line)
                if m:
                    private_methods.add(m.group(1))
        return private_methods

    # ── calls ─────────────────────────────────────────────────────

    def _parse_calls(self, lines: list[str]) -> list[Call]:
        calls: list[Call] = []
        _call = re.compile(r'([\w.:]+(?:\.\w+)*)\s*[\(]')
        _new_call = re.compile(r'(\w[\w:]*)\.(new)\b')
        _keywords = {
            "def", "class", "module", "if", "unless", "while", "until",
            "for", "case", "when", "begin", "rescue", "ensure", "end",
            "return", "yield", "raise", "require", "require_relative",
            "include", "extend", "puts", "print",
        }

        for i, line in enumerate(lines):
            for m in _call.finditer(line):
                name = m.group(1)
                base = name.split(".")[-1] if "." in name else name
                if base not in _keywords:
                    calls.append(Call(name=name, line=i + 1))
            for m in _new_call.finditer(line):
                calls.append(Call(name=f"{m.group(1)}.new", line=i + 1))
        return calls
