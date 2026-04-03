from __future__ import annotations

import re
from pathlib import Path

from .base import Parser
from ..finding import Call, Class, Export, Function, Import, ParsedFile, Variable

try:
    import tree_sitter_java as tsjava
    from tree_sitter import Language, Parser as TSParser
    JAVA_LANGUAGE = Language(tsjava.language())
except ImportError:
    JAVA_LANGUAGE = None


class JavaParser(Parser):
    """Parse Java files using tree-sitter-java with regex fallback."""

    def supports(self, path: Path) -> bool:
        return path.suffix == ".java"

    def parse_file(self, path: Path) -> ParsedFile:
        source = path.read_text(encoding="utf-8", errors="replace")
        raw_lines = source.count("\n") + (1 if source and not source.endswith("\n") else 0)

        try:
            if JAVA_LANGUAGE is not None:
                return self._parse_tree_sitter(path, source, raw_lines)
            return self._parse_regex(path, source, raw_lines)
        except Exception:
            return ParsedFile(path=path, raw_lines=raw_lines)

    # ── tree-sitter ──────────────────────────────────────────────

    def _parse_tree_sitter(self, path: Path, source: str, raw_lines: int) -> ParsedFile:
        parser = TSParser(JAVA_LANGUAGE)
        tree = parser.parse(source.encode("utf-8"))
        root = tree.root_node

        imports = self._ts_imports(root)
        functions = self._ts_functions(root)
        classes = self._ts_classes(root)
        variables = self._ts_variables(root)
        calls = self._ts_calls(root)
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

    def _ts_imports(self, root) -> list[Import]:
        imports: list[Import] = []
        for node in self._walk(root):
            if node.type == "import_declaration":
                text = node.text.decode("utf-8").strip().rstrip(";")
                # import static org.junit.Assert.assertEquals
                # import java.util.List
                is_static = "static " in text
                parts = text.split()
                # parts: ['import', 'java.util.List'] or ['import', 'static', 'org...']
                imp_path = parts[-1]
                short_name = imp_path.rsplit(".", 1)[-1]
                source = imp_path.rsplit(".", 1)[0] if "." in imp_path else None
                imports.append(Import(
                    name=short_name,
                    source=imp_path,
                    line=node.start_point[0] + 1,
                    alias="static" if is_static else None,
                ))
        return imports

    def _ts_functions(self, root) -> list[Function]:
        functions: list[Function] = []
        for node in self._walk(root):
            if node.type == "method_declaration" or node.type == "constructor_declaration":
                name_node = node.child_by_field_name("name")
                if name_node:
                    name = name_node.text.decode("utf-8")
                    line = node.start_point[0] + 1
                    end_line = node.end_point[0] + 1
                    params = node.child_by_field_name("parameters")
                    param_count = self._ts_param_count(params)
                    functions.append(Function(
                        name=name,
                        line=line,
                        end_line=end_line,
                        param_count=param_count,
                        line_count=end_line - line + 1,
                    ))
        return functions

    def _ts_param_count(self, params_node) -> int:
        if not params_node:
            return 0
        count = 0
        for child in params_node.children:
            if child.type == "formal_parameter" or child.type == "spread_parameter":
                count += 1
        return count

    def _ts_classes(self, root) -> list[Class]:
        classes: list[Class] = []
        for node in self._walk(root):
            if node.type in ("class_declaration", "interface_declaration",
                             "enum_declaration", "record_declaration",
                             "annotation_type_declaration"):
                name_node = node.child_by_field_name("name")
                if name_node:
                    name = name_node.text.decode("utf-8")
                    line = node.start_point[0] + 1
                    end_line = node.end_point[0] + 1
                    method_count = self._count_methods_in(node)
                    classes.append(Class(
                        name=name,
                        line=line,
                        end_line=end_line,
                        method_count=method_count,
                        line_count=end_line - line + 1,
                    ))
        return classes

    def _count_methods_in(self, class_node) -> int:
        count = 0
        body = class_node.child_by_field_name("body")
        if not body:
            return 0
        for child in body.children:
            if child.type in ("method_declaration", "constructor_declaration"):
                count += 1
        return count

    def _ts_variables(self, root) -> list[Variable]:
        variables: list[Variable] = []
        for node in self._walk(root):
            if node.type == "field_declaration":
                declarator = None
                for child in node.children:
                    if child.type == "variable_declarator":
                        name_node = child.child_by_field_name("name")
                        if name_node:
                            variables.append(Variable(
                                name=name_node.text.decode("utf-8"),
                                line=node.start_point[0] + 1,
                                scope="class",
                            ))
        return variables

    def _ts_calls(self, root) -> list[Call]:
        calls: list[Call] = []
        for node in self._walk(root):
            if node.type == "method_invocation":
                name_node = node.child_by_field_name("name")
                obj_node = node.child_by_field_name("object")
                if name_node:
                    name = name_node.text.decode("utf-8")
                    if obj_node:
                        name = f"{obj_node.text.decode('utf-8')}.{name}"
                    calls.append(Call(name=name, line=node.start_point[0] + 1))
            elif node.type == "object_creation_expression":
                type_node = node.child_by_field_name("type")
                if type_node:
                    name = f"new {type_node.text.decode('utf-8')}"
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
        clean_lines = self._strip_comments(lines)

        imports = self._re_imports(lines)  # use original lines for imports
        functions = self._re_functions(clean_lines)
        classes = self._re_classes(clean_lines, functions)
        variables = self._re_variables(clean_lines)
        calls = self._re_calls(clean_lines)
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

    def _strip_comments(self, lines: list[str]) -> list[str]:
        """Remove // and /* */ comments, preserving line numbers."""
        result: list[str] = []
        in_block = False
        for line in lines:
            out = []
            i = 0
            while i < len(line):
                if in_block:
                    end = line.find("*/", i)
                    if end == -1:
                        break
                    i = end + 2
                    in_block = False
                    continue
                if i + 1 < len(line):
                    two = line[i:i + 2]
                    if two == "//":
                        break
                    if two == "/*":
                        in_block = True
                        i += 2
                        continue
                # Skip string literals to avoid false matches
                if line[i] == '"':
                    end = i + 1
                    while end < len(line) and line[end] != '"':
                        if line[end] == '\\':
                            end += 1
                        end += 1
                    out.append(line[i:end + 1])
                    i = end + 1
                    continue
                out.append(line[i])
                i += 1
            result.append("".join(out))
        return result

    def _re_imports(self, lines: list[str]) -> list[Import]:
        imports: list[Import] = []
        _import = re.compile(
            r'^\s*import\s+(static\s+)?([\w.*]+)\s*;'
        )
        for i, line in enumerate(lines):
            m = _import.match(line)
            if m:
                is_static = m.group(1) is not None
                imp_path = m.group(2)
                short_name = imp_path.rsplit(".", 1)[-1]
                imports.append(Import(
                    name=short_name,
                    source=imp_path,
                    line=i + 1,
                    alias="static" if is_static else None,
                ))
        return imports

    def _re_functions(self, lines: list[str]) -> list[Function]:
        functions: list[Function] = []
        _method = re.compile(
            r'^\s*'
            r'(?:(?:public|private|protected|static|final|abstract|native|'
            r'synchronized|strictfp|default|transient|volatile)\s+)*'
            r'(?:<[^>]+>\s+)?'  # generic type params
            r'(?:[\w\[\]<>,\s?]+)\s+'  # return type
            r'(\w+)\s*\(([^)]*)\)'  # method name + params
            r'\s*(?:throws\s+[\w,\s]+)?'  # optional throws
            r'\s*[{;]'  # body or abstract
        )
        # Constructor pattern: ClassName(params) {
        _ctor = re.compile(
            r'^\s*(?:(?:public|private|protected)\s+)?'
            r'([A-Z]\w*)\s*\(([^)]*)\)\s*(?:throws\s+[\w,\s]+)?\s*\{'
        )
        for i, line in enumerate(lines):
            stripped = line.strip()
            if not stripped or stripped.startswith("@"):
                continue
            m = _method.match(line)
            if m:
                name = m.group(1)
                params = m.group(2)
                # Skip control flow keywords that look like method calls
                if name in ("if", "for", "while", "switch", "catch", "return", "new", "class"):
                    continue
                param_count = self._count_params(params)
                end_line = self._find_block_end(lines, i) if "{" in line else i
                functions.append(Function(
                    name=name,
                    line=i + 1,
                    end_line=end_line + 1,
                    param_count=param_count,
                    line_count=end_line - i + 1,
                ))
                continue
            m = _ctor.match(line)
            if m:
                name = m.group(1)
                params = m.group(2)
                param_count = self._count_params(params)
                end_line = self._find_block_end(lines, i)
                functions.append(Function(
                    name=name,
                    line=i + 1,
                    end_line=end_line + 1,
                    param_count=param_count,
                    line_count=end_line - i + 1,
                ))
        return functions

    def _re_classes(self, lines: list[str], functions: list[Function]) -> list[Class]:
        classes: list[Class] = []
        _class = re.compile(
            r'^\s*(?:(?:public|private|protected|static|final|abstract|strictfp)\s+)*'
            r'(?:class|interface|enum|record|@interface)\s+'
            r'(\w+)'
        )
        for i, line in enumerate(lines):
            m = _class.match(line)
            if m:
                name = m.group(1)
                end_line = self._find_block_end(lines, i)
                # Count methods whose line range falls within this class
                method_count = sum(
                    1 for fn in functions
                    if (i + 1) <= fn.line <= (end_line + 1)
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
        _field = re.compile(
            r'^\s*(?:(?:public|private|protected|static|final|transient|volatile)\s+)+'
            r'(?:[\w\[\]<>,\s?]+)\s+'
            r'(\w+)\s*[;=]'
        )
        for i, line in enumerate(lines):
            m = _field.match(line)
            if m:
                name = m.group(1)
                variables.append(Variable(
                    name=name,
                    line=i + 1,
                    scope="class",
                ))
        return variables

    def _re_calls(self, lines: list[str]) -> list[Call]:
        calls: list[Call] = []
        _call = re.compile(r'((?:[\w]+\.)*[\w]+)\s*\(')
        _new = re.compile(r'\bnew\s+([\w.]+)\s*\(')
        _keywords = {
            "if", "for", "while", "switch", "catch", "return", "class",
            "interface", "enum", "record", "import", "package", "throws",
            "extends", "implements", "super", "assert",
        }
        for i, line in enumerate(lines):
            for m in _new.finditer(line):
                calls.append(Call(name=f"new {m.group(1)}", line=i + 1))
            for m in _call.finditer(line):
                name = m.group(1)
                base = name.split(".")[-1]
                if base not in _keywords and not base[0:1].isupper() or "." in name:
                    # Method calls (lowercase start) or qualified calls
                    if base not in _keywords:
                        calls.append(Call(name=name, line=i + 1))
        return calls

    # ── shared helpers ───────────────────────────────────────────

    def _count_params(self, params_str: str) -> int:
        params_str = params_str.strip()
        if not params_str:
            return 0
        return len([p for p in params_str.split(",") if p.strip()])

    def _find_block_end(self, lines: list[str], start: int) -> int:
        """Find the closing brace of a block starting at `start`."""
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

    def _build_exports(
        self,
        functions: list[Function],
        classes: list[Class],
        variables: list[Variable],
    ) -> list[Export]:
        """In Java, public members are exports."""
        # Since we can't easily re-check modifiers from stored data,
        # we mark all top-level classes and their public methods as exports.
        # For regex mode, we re-scan is not feasible, so we export all
        # classes and functions found (the regex already filters by
        # visibility via modifier patterns for variables).
        exports: list[Export] = []
        for cls in classes:
            exports.append(Export(name=cls.name, line=cls.line, kind="class"))
        for fn in functions:
            exports.append(Export(name=fn.name, line=fn.line, kind="function"))
        return exports
