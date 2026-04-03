from __future__ import annotations

import re
from pathlib import Path

from .base import Parser
from ..finding import Call, Class, Export, Function, Import, ParsedFile, Variable


class ObjCParser(Parser):
    """Parse Objective-C files using regex-based parsing."""

    def supports(self, path: Path) -> bool:
        return path.suffix in (".m", ".mm")

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
        classes = self._parse_classes(lines, functions)
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
        # Remove block comments /* ... */
        source = re.sub(r'/\*.*?\*/', '', source, flags=re.DOTALL)
        # Remove line comments // ...
        source = re.sub(r'//[^\n]*', '', source)
        return source

    # ── imports ───────────────────────────────────────────────────

    def _parse_imports(self, lines: list[str]) -> list[Import]:
        imports: list[Import] = []
        # #import <Framework/Header.h>
        _angle = re.compile(r'^#(?:import|include)\s+<([^>]+)>')
        # #import "Header.h"
        _quote = re.compile(r'^#(?:import|include)\s+"([^"]+)"')
        # @import UIKit;
        _at_import = re.compile(r'^@import\s+([\w.]+)\s*;')

        for i, line in enumerate(lines):
            stripped = line.strip()

            m = _angle.match(stripped)
            if m:
                path_str = m.group(1)
                name = path_str.split("/")[-1] if "/" in path_str else path_str
                imports.append(Import(
                    name=name,
                    source=path_str,
                    line=i + 1,
                ))
                continue

            m = _quote.match(stripped)
            if m:
                path_str = m.group(1)
                name = path_str.split("/")[-1] if "/" in path_str else path_str
                imports.append(Import(
                    name=name,
                    source=path_str,
                    line=i + 1,
                ))
                continue

            m = _at_import.match(stripped)
            if m:
                module = m.group(1)
                imports.append(Import(
                    name=module,
                    source=module,
                    line=i + 1,
                ))

        return imports

    # ── functions ─────────────────────────────────────────────────

    def _parse_functions(self, lines: list[str]) -> list[Function]:
        functions: list[Function] = []

        # Objective-C method: - (void)foo:(NSString *)bar baz:(int)qux
        _objc_method = re.compile(
            r'^[+-]\s*\([^)]*\)\s*(\w[\w:]*(?:\s*\([^)]*\)\s*\w+\s*\w*[\w:]*)*)'
        )
        # C-style function: void foo(int x, char *y)
        _c_func = re.compile(
            r'^(?:(?:static|inline|extern|void|int|float|double|char|long|short|unsigned|signed|'
            r'NSInteger|NSUInteger|CGFloat|BOOL|id|instancetype|NSString|NSArray|NSDictionary|'
            r'__attribute__\([^)]*\))\s+)*'
            r'(?:\w+\s*\*?\s+)*'
            r'(\w+)\s*\(([^)]*)\)\s*\{'
        )

        for i, line in enumerate(lines):
            stripped = line.strip()

            # Try ObjC method first
            m = _objc_method.match(stripped)
            if m:
                selector_raw = stripped
                # Extract selector parts (name portions with colons)
                # e.g. - (void)foo:(NSString *)bar baz:(int)qux -> foo:baz:
                # e.g. - (void)foo -> foo
                after_return = re.match(r'^[+-]\s*\([^)]*\)\s*(.*)', selector_raw)
                if after_return:
                    rest = after_return.group(1).split("{")[0].strip().rstrip(";")
                    selector = self._extract_selector(rest)
                    param_count = selector.count(":") if ":" in selector else 0
                    end_line = self._find_brace_end(lines, i) if "{" in line or self._has_brace_ahead(lines, i) else i
                    functions.append(Function(
                        name=selector,
                        line=i + 1,
                        end_line=end_line + 1,
                        param_count=param_count,
                        line_count=end_line - i + 1,
                    ))
                continue

            # Try C function
            m = _c_func.match(stripped)
            if m:
                name = m.group(1)
                params_str = m.group(2)
                # Skip common keywords that look like functions
                if name in ("if", "for", "while", "switch", "return", "sizeof"):
                    continue
                param_count = self._count_c_params(params_str)
                end_line = self._find_brace_end(lines, i)
                functions.append(Function(
                    name=name,
                    line=i + 1,
                    end_line=end_line + 1,
                    param_count=param_count,
                    line_count=end_line - i + 1,
                ))

        return functions

    def _extract_selector(self, rest: str) -> str:
        """Extract ObjC selector from method signature text after return type.

        Examples:
            'foo' -> 'foo'
            'foo:(NSString *)bar' -> 'foo:'
            'foo:(NSString *)bar baz:(int)qux' -> 'foo:baz:'
        """
        if not rest:
            return ""
        # Remove everything inside parens (types)
        cleaned = re.sub(r'\([^)]*\)', '', rest)
        parts = cleaned.split()
        selector_parts = []
        for part in parts:
            part = part.strip()
            if not part:
                continue
            if part.endswith(":"):
                selector_parts.append(part)
            elif ":" in part:
                # Something like "foo:bar" split
                selector_parts.append(part.split(":")[0] + ":")
            elif not selector_parts:
                # First word, no colon = unary selector
                selector_parts.append(part)
            # else: parameter name, skip
        return "".join(selector_parts)

    def _count_c_params(self, params_str: str) -> int:
        params_str = params_str.strip()
        if not params_str or params_str == "void":
            return 0
        return len([p for p in params_str.split(",") if p.strip()])

    def _has_brace_ahead(self, lines: list[str], start: int) -> bool:
        """Check if an opening brace appears within a few lines."""
        for i in range(start, min(start + 3, len(lines))):
            if "{" in lines[i]:
                return True
        return False

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

    def _parse_classes(self, lines: list[str], functions: list[Function]) -> list[Class]:
        classes: list[Class] = []
        # @interface Foo, @interface Foo (Category), @interface Foo : NSObject
        _interface = re.compile(
            r'^@(?:interface|implementation)\s+(\w+)\s*(?:\((\w*)\))?'
        )
        _protocol = re.compile(r'^@protocol\s+(\w+)')
        _end = re.compile(r'^@end\b')

        i = 0
        while i < len(lines):
            stripped = lines[i].strip()

            m = _interface.match(stripped)
            if not m:
                m = _protocol.match(stripped)

            if m:
                name = m.group(1)
                category = m.group(2) if m.lastindex and m.lastindex >= 2 else None
                if category:
                    name = f"{name}({category})" if category else name
                start_line = i

                # Find @end
                end_line = i
                for j in range(i + 1, len(lines)):
                    if _end.match(lines[j].strip()):
                        end_line = j
                        break

                # Count methods inside
                method_count = 0
                _method_sig = re.compile(r'^\s*[+-]\s*\(')
                for j in range(start_line + 1, end_line):
                    if _method_sig.match(lines[j]):
                        method_count += 1

                classes.append(Class(
                    name=name,
                    line=start_line + 1,
                    end_line=end_line + 1,
                    method_count=method_count,
                    line_count=end_line - start_line + 1,
                ))
                i = end_line + 1
                continue

            i += 1

        return classes

    # ── variables ─────────────────────────────────────────────────

    def _parse_variables(self, lines: list[str]) -> list[Variable]:
        variables: list[Variable] = []
        # @property (nonatomic, strong) NSString *name;
        _property = re.compile(
            r'^@property\s*\([^)]*\)\s+[\w\s]+\s*\*?\s*(\w+)\s*;'
        )
        # static NSString *const kFoo = @"bar";
        _static_var = re.compile(
            r'^static\s+[\w\s]+\s*\*\s*(?:const\s+)?(\w+)\s*='
        )
        # Global C variables: NSString *foo = ...;
        _global_var = re.compile(
            r'^(?:extern\s+)?(?:const\s+)?(?:\w+\s+)+\*?\s*(\w+)\s*=\s*'
        )

        for i, line in enumerate(lines):
            stripped = line.strip()

            m = _property.match(stripped)
            if m:
                variables.append(Variable(
                    name=m.group(1),
                    line=i + 1,
                    scope="class",
                ))
                continue

            m = _static_var.match(stripped)
            if m:
                name = m.group(1)
                variables.append(Variable(
                    name=name,
                    line=i + 1,
                    scope="module",
                ))

        return variables

    # ── calls ─────────────────────────────────────────────────────

    def _parse_calls(self, lines: list[str]) -> list[Call]:
        calls: list[Call] = []
        # ObjC message send: [obj method] or [obj method:arg]
        _msg_send = re.compile(r'\[(\w+)\s+(\w+)')
        # C function call: foo(...)
        _c_call = re.compile(r'(\w+)\s*\(')
        _keywords = {
            "if", "for", "while", "switch", "return", "sizeof", "typeof",
            "static", "extern", "const", "void", "int", "float", "double",
            "char", "long", "short", "unsigned", "signed", "struct", "enum",
            "union", "typedef", "case", "default", "else",
        }

        for i, line in enumerate(lines):
            # ObjC message sends
            for m in _msg_send.finditer(line):
                receiver = m.group(1)
                method = m.group(2)
                calls.append(Call(
                    name=f"[{receiver} {method}]",
                    line=i + 1,
                ))

            # C function calls
            for m in _c_call.finditer(line):
                name = m.group(1)
                if name not in _keywords:
                    calls.append(Call(name=name, line=i + 1))

        return calls

    # ── exports ───────────────────────────────────────────────────

    def _build_exports(
        self,
        functions: list[Function],
        classes: list[Class],
        variables: list[Variable],
    ) -> list[Export]:
        """Non-static functions, all classes/protocols. Methods starting with _ are private."""
        exports: list[Export] = []

        for fn in functions:
            # Skip conventionally private methods
            bare_name = fn.name.lstrip("+-").strip()
            if bare_name.startswith("_"):
                continue
            exports.append(Export(name=fn.name, line=fn.line, kind="function"))

        for cls in classes:
            exports.append(Export(name=cls.name, line=cls.line, kind="class"))

        for var in variables:
            if var.scope == "module":
                exports.append(Export(name=var.name, line=var.line, kind="variable"))

        return exports
