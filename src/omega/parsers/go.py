from __future__ import annotations

import re
from pathlib import Path

from .base import Parser
from ..finding import Call, Class, Export, Function, Import, ParsedFile, Variable

try:
    import tree_sitter_go as tsgo
    from tree_sitter import Language, Parser as TSParser
    GO_LANGUAGE = Language(tsgo.language())
except ImportError:
    GO_LANGUAGE = None


class GoParser(Parser):
    """Parse Go files using tree-sitter-go with regex fallback."""

    def supports(self, path: Path) -> bool:
        return path.suffix == ".go"

    def parse_file(self, path: Path) -> ParsedFile:
        source = path.read_text(encoding="utf-8", errors="replace")
        raw_lines = source.count("\n") + (1 if source and not source.endswith("\n") else 0)

        try:
            if GO_LANGUAGE is not None:
                return self._parse_tree_sitter(path, source, raw_lines)
            return self._parse_regex(path, source, raw_lines)
        except Exception:
            return ParsedFile(path=path, raw_lines=raw_lines)

    # ── tree-sitter ──────────────────────────────────────────────

    def _parse_tree_sitter(self, path: Path, source: str, raw_lines: int) -> ParsedFile:
        parser = TSParser(GO_LANGUAGE)
        tree = parser.parse(source.encode("utf-8"))
        root = tree.root_node

        imports = self._ts_imports(root, source)
        functions = self._ts_functions(root, source)
        classes = self._ts_classes(root, source, functions)
        variables = self._ts_variables(root, source)
        calls = self._ts_calls(root, source)
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

    def _ts_imports(self, root, source: str) -> list[Import]:
        imports: list[Import] = []
        for node in self._walk(root):
            if node.type == "import_spec":
                path_node = node.child_by_field_name("path")
                if path_node:
                    imp_path = path_node.text.decode("utf-8").strip('"')
                    name_node = node.child_by_field_name("name")
                    alias = name_node.text.decode("utf-8") if name_node else None
                    short_name = imp_path.rsplit("/", 1)[-1]
                    imports.append(Import(
                        name=short_name,
                        source=imp_path,
                        line=node.start_point[0] + 1,
                        alias=alias,
                    ))
        return imports

    def _ts_functions(self, root, source: str) -> list[Function]:
        functions: list[Function] = []
        for node in self._walk(root):
            if node.type == "function_declaration":
                name_node = node.child_by_field_name("name")
                if name_node:
                    name = name_node.text.decode("utf-8")
                    line = node.start_point[0] + 1
                    end_line = node.end_point[0] + 1
                    param_count = self._ts_param_count(node)
                    functions.append(Function(
                        name=name,
                        line=line,
                        end_line=end_line,
                        param_count=param_count,
                        line_count=end_line - line + 1,
                    ))
            elif node.type == "method_declaration":
                name_node = node.child_by_field_name("name")
                if name_node:
                    name = name_node.text.decode("utf-8")
                    receiver = self._ts_receiver_type(node)
                    full_name = f"{receiver}.{name}" if receiver else name
                    line = node.start_point[0] + 1
                    end_line = node.end_point[0] + 1
                    param_count = self._ts_param_count(node)
                    functions.append(Function(
                        name=full_name,
                        line=line,
                        end_line=end_line,
                        param_count=param_count,
                        line_count=end_line - line + 1,
                    ))
        return functions

    def _ts_receiver_type(self, node) -> str | None:
        receiver = node.child_by_field_name("receiver")
        if receiver:
            for child in self._walk(receiver):
                if child.type == "type_identifier":
                    return child.text.decode("utf-8")
        return None

    def _ts_param_count(self, node) -> int:
        params = node.child_by_field_name("parameters")
        if not params:
            return 0
        count = 0
        for child in params.children:
            if child.type == "parameter_declaration":
                # A parameter_declaration can declare multiple names
                names = [c for c in child.children if c.type == "identifier"]
                count += max(len(names), 1)
        return count

    def _ts_classes(self, root, source: str, functions: list[Function]) -> list[Class]:
        classes: list[Class] = []
        for node in self._walk(root):
            if node.type == "type_declaration":
                for child in node.children:
                    if child.type == "type_spec":
                        name_node = child.child_by_field_name("name")
                        type_node = child.child_by_field_name("type")
                        if name_node and type_node and type_node.type == "struct_type":
                            name = name_node.text.decode("utf-8")
                            line = node.start_point[0] + 1
                            end_line = node.end_point[0] + 1
                            method_count = sum(
                                1 for fn in functions
                                if fn.name.startswith(f"{name}.")
                            )
                            classes.append(Class(
                                name=name,
                                line=line,
                                end_line=end_line,
                                method_count=method_count,
                                line_count=end_line - line + 1,
                            ))
        return classes

    def _ts_variables(self, root, source: str) -> list[Variable]:
        variables: list[Variable] = []
        for node in root.children:
            if node.type in ("var_declaration", "const_declaration"):
                for child in node.children:
                    if child.type == "var_spec" or child.type == "const_spec":
                        name_node = child.child_by_field_name("name")
                        if name_node:
                            variables.append(Variable(
                                name=name_node.text.decode("utf-8"),
                                line=node.start_point[0] + 1,
                                scope="module",
                            ))
                        else:
                            # Multiple names in a spec
                            for c in child.children:
                                if c.type == "identifier":
                                    variables.append(Variable(
                                        name=c.text.decode("utf-8"),
                                        line=c.start_point[0] + 1,
                                        scope="module",
                                    ))
        return variables

    def _ts_calls(self, root, source: str) -> list[Call]:
        calls: list[Call] = []
        for node in self._walk(root):
            if node.type == "call_expression":
                func_node = node.child_by_field_name("function")
                if func_node:
                    name = func_node.text.decode("utf-8")
                    calls.append(Call(name=name, line=node.start_point[0] + 1))
        return calls

    def _walk(self, node):
        """Depth-first walk of all tree-sitter nodes."""
        yield node
        for child in node.children:
            yield from self._walk(child)

    # ── regex fallback ───────────────────────────────────────────

    def _parse_regex(self, path: Path, source: str, raw_lines: int) -> ParsedFile:
        lines = source.splitlines()

        imports = self._re_imports(lines)
        functions = self._re_functions(lines)
        classes = self._re_classes(lines, functions)
        variables = self._re_variables(lines)
        calls = self._re_calls(lines)
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

    def _re_imports(self, lines: list[str]) -> list[Import]:
        imports: list[Import] = []
        in_block = False
        _single = re.compile(r'^import\s+"([^"]+)"')
        _block_start = re.compile(r'^import\s*\(')
        _block_line = re.compile(r'^\s*(?:(\w+)\s+)?"([^"]+)"')

        for i, line in enumerate(lines):
            stripped = line.strip()
            if in_block:
                if stripped == ")":
                    in_block = False
                    continue
                m = _block_line.match(stripped)
                if m:
                    alias, imp_path = m.group(1), m.group(2)
                    short_name = imp_path.rsplit("/", 1)[-1]
                    imports.append(Import(
                        name=short_name,
                        source=imp_path,
                        line=i + 1,
                        alias=alias,
                    ))
                continue

            m = _single.match(stripped)
            if m:
                imp_path = m.group(1)
                short_name = imp_path.rsplit("/", 1)[-1]
                imports.append(Import(
                    name=short_name,
                    source=imp_path,
                    line=i + 1,
                ))
                continue

            if _block_start.match(stripped):
                in_block = True
        return imports

    def _re_functions(self, lines: list[str]) -> list[Function]:
        functions: list[Function] = []
        _func = re.compile(
            r'^func\s+'
            r'(?:\((\w+)\s+\*?(\w+)\)\s+)?'  # optional receiver
            r'(\w+)\s*\(([^)]*)\)'
        )
        for i, line in enumerate(lines):
            m = _func.match(line.strip())
            if m:
                recv_var, recv_type, name, params = m.groups()
                if recv_type:
                    full_name = f"{recv_type}.{name}"
                else:
                    full_name = name
                param_count = self._count_params(params)
                end_line = self._find_func_end(lines, i)
                functions.append(Function(
                    name=full_name,
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

    def _find_func_end(self, lines: list[str], start: int) -> int:
        """Find the closing brace of a function starting at `start`."""
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

    def _re_classes(self, lines: list[str], functions: list[Function]) -> list[Class]:
        classes: list[Class] = []
        _struct = re.compile(r'^type\s+(\w+)\s+struct\b')
        for i, line in enumerate(lines):
            m = _struct.match(line.strip())
            if m:
                name = m.group(1)
                end_line = self._find_func_end(lines, i)
                method_count = sum(
                    1 for fn in functions
                    if fn.name.startswith(f"{name}.")
                )
                classes.append(Class(
                    name=name,
                    line=i + 1,
                    end_line=end_line + 1,
                    method_count=method_count,
                    line_count=end_line - i + 1,
                ))
        return classes

    def _re_variables(self, lines: list[str]) -> list[Variable]:
        variables: list[Variable] = []
        _var = re.compile(r'^(?:var|const)\s+(\w+)\b')
        _block_start = re.compile(r'^(?:var|const)\s*\(')
        _block_name = re.compile(r'^\s*(\w+)\b')
        in_block = False

        for i, line in enumerate(lines):
            stripped = line.strip()
            if in_block:
                if stripped == ")":
                    in_block = False
                    continue
                m = _block_name.match(stripped)
                if m and not stripped.startswith("//"):
                    variables.append(Variable(
                        name=m.group(1), line=i + 1, scope="module",
                    ))
                continue

            if _block_start.match(stripped):
                in_block = True
                continue

            m = _var.match(stripped)
            if m:
                variables.append(Variable(
                    name=m.group(1), line=i + 1, scope="module",
                ))
        return variables

    def _re_calls(self, lines: list[str]) -> list[Call]:
        calls: list[Call] = []
        _call = re.compile(r'([\w.]+)\s*\(')
        _keywords = {"func", "if", "for", "switch", "select", "return", "go", "defer", "range"}
        for i, line in enumerate(lines):
            for m in _call.finditer(line):
                name = m.group(1)
                if name not in _keywords and not name.startswith("//"):
                    calls.append(Call(name=name, line=i + 1))
        return calls

    # ── shared helpers ───────────────────────────────────────────

    def _build_exports(
        self,
        functions: list[Function],
        classes: list[Class],
        variables: list[Variable],
    ) -> list[Export]:
        """Go exports anything with a PascalCase (uppercase first letter) name."""
        exports: list[Export] = []
        for fn in functions:
            # For methods like Server.Listen, check the method name part
            short_name = fn.name.rsplit(".", 1)[-1]
            if short_name[:1].isupper():
                exports.append(Export(name=fn.name, line=fn.line, kind="function"))
        for cls in classes:
            if cls.name[:1].isupper():
                exports.append(Export(name=cls.name, line=cls.line, kind="class"))
        for var in variables:
            if var.name[:1].isupper():
                exports.append(Export(name=var.name, line=var.line, kind="variable"))
        return exports
