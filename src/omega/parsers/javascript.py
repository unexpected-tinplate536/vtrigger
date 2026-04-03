from __future__ import annotations

from pathlib import Path

import tree_sitter_javascript as tsjs
import tree_sitter_typescript as tsts
from tree_sitter import Language, Node, Parser

from .base import Parser as BaseParser
from ..finding import Call, Class, Export, Function, Import, ParsedFile, Variable

JS_LANGUAGE = Language(tsjs.language())
TS_LANGUAGE = Language(tsts.language_typescript())
TSX_LANGUAGE = Language(tsts.language_tsx())

_LANG_MAP = {
    ".js": JS_LANGUAGE,
    ".jsx": JS_LANGUAGE,
    ".ts": TS_LANGUAGE,
    ".tsx": TSX_LANGUAGE,
}


class JavaScriptParser(BaseParser):
    """Parse JavaScript/TypeScript files using tree-sitter."""

    def supports(self, path: Path) -> bool:
        return path.suffix in _LANG_MAP

    def parse_file(self, path: Path) -> ParsedFile:
        source = path.read_text(encoding="utf-8", errors="replace")
        raw_lines = source.count("\n") + (1 if source and not source.endswith("\n") else 0)

        lang = _LANG_MAP.get(path.suffix)
        if lang is None:
            return ParsedFile(path=path, raw_lines=raw_lines)

        parser = Parser(lang)

        try:
            tree = parser.parse(source.encode("utf-8"))
        except Exception:
            return ParsedFile(path=path, raw_lines=raw_lines)

        if tree.root_node.has_error:
            # Still try to extract what we can; tree-sitter is error-tolerant.
            pass

        root = tree.root_node

        imports = self._extract_imports(root)
        exports = self._extract_exports(root)
        functions = self._extract_functions(root)
        classes = self._extract_classes(root)
        variables = self._extract_variables(root)
        calls = self._extract_calls(root)

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

    # ------------------------------------------------------------------
    # Imports
    # ------------------------------------------------------------------

    def _extract_imports(self, root: Node) -> list[Import]:
        imports: list[Import] = []
        for child in root.children:
            if child.type == "import_statement":
                imports.extend(self._parse_import_statement(child))
            elif child.type in ("lexical_declaration", "variable_declaration"):
                imports.extend(self._parse_require_declaration(child))
        return imports

    def _parse_import_statement(self, node: Node) -> list[Import]:
        imports: list[Import] = []
        source = self._import_source(node)
        if source is None:
            return imports

        for child in node.children:
            if child.type == "import_clause":
                imports.extend(self._parse_import_clause(child, source))
        # Side-effect import: `import 'foo'` (no clause)
        if not imports:
            # Check if there's simply no import clause (side-effect only)
            has_clause = any(c.type == "import_clause" for c in node.children)
            if not has_clause:
                imports.append(Import(
                    name="*",
                    source=source,
                    line=node.start_point[0] + 1,
                ))
        return imports

    def _parse_import_clause(self, node: Node, source: str) -> list[Import]:
        imports: list[Import] = []
        line = node.start_point[0] + 1

        for child in node.children:
            if child.type == "identifier":
                # default import: `import X from '...'`
                imports.append(Import(
                    name=child.text.decode("utf-8"),
                    source=source,
                    line=line,
                ))
            elif child.type == "named_imports":
                for spec in child.children:
                    if spec.type == "import_specifier":
                        imports.append(self._parse_import_specifier(spec, source, line))
            elif child.type == "namespace_import":
                # `import * as X from '...'`
                alias_node = None
                for c in child.children:
                    if c.type == "identifier":
                        alias_node = c
                alias = alias_node.text.decode("utf-8") if alias_node else None
                imports.append(Import(name="*", source=source, line=line, alias=alias))
        return imports

    def _parse_import_specifier(self, node: Node, source: str, line: int) -> Import:
        identifiers = [c for c in node.children if c.type == "identifier"]
        if len(identifiers) >= 2:
            name = identifiers[0].text.decode("utf-8")
            alias = identifiers[1].text.decode("utf-8")
            return Import(name=name, source=source, line=line, alias=alias)
        elif identifiers:
            name = identifiers[0].text.decode("utf-8")
            return Import(name=name, source=source, line=line)
        return Import(name="", source=source, line=line)

    def _import_source(self, node: Node) -> str | None:
        for child in node.children:
            if child.type == "string":
                raw = child.text.decode("utf-8")
                return raw.strip("'\"")
        return None

    def _parse_require_declaration(self, node: Node) -> list[Import]:
        """Extract `const X = require('Y')` patterns."""
        imports: list[Import] = []
        for child in node.children:
            if child.type == "variable_declarator":
                name_node = child.child_by_field_name("name")
                value_node = child.child_by_field_name("value")
                if name_node and value_node and value_node.type == "call_expression":
                    func_node = value_node.child_by_field_name("function")
                    if func_node and func_node.type == "identifier" and func_node.text == b"require":
                        args_node = value_node.child_by_field_name("arguments")
                        if args_node:
                            for arg in args_node.children:
                                if arg.type == "string":
                                    source = arg.text.decode("utf-8").strip("'\"")
                                    imports.append(Import(
                                        name=name_node.text.decode("utf-8"),
                                        source=source,
                                        line=node.start_point[0] + 1,
                                    ))
                                    break
        return imports

    # ------------------------------------------------------------------
    # Exports
    # ------------------------------------------------------------------

    def _extract_exports(self, root: Node) -> list[Export]:
        exports: list[Export] = []
        for child in root.children:
            if child.type == "export_statement":
                exports.extend(self._parse_export_statement(child))
        return exports

    def _parse_export_statement(self, node: Node) -> list[Export]:
        exports: list[Export] = []
        line = node.start_point[0] + 1
        is_default = any(
            c.type == "default" or (c.type is not None and c.text == b"default")
            for c in node.children
        )

        for child in node.children:
            if child.type == "function_declaration":
                name_node = child.child_by_field_name("name")
                name = name_node.text.decode("utf-8") if name_node else "default"
                kind = "default" if is_default else "function"
                exports.append(Export(name=name, line=line, kind=kind))
            elif child.type == "class_declaration":
                name_node = child.child_by_field_name("name")
                name = name_node.text.decode("utf-8") if name_node else "default"
                kind = "default" if is_default else "class"
                exports.append(Export(name=name, line=line, kind=kind))
            elif child.type in ("lexical_declaration", "variable_declaration"):
                for decl in child.children:
                    if decl.type == "variable_declarator":
                        name_node = decl.child_by_field_name("name")
                        if name_node:
                            exports.append(Export(
                                name=name_node.text.decode("utf-8"),
                                line=line,
                                kind="variable",
                            ))
            elif child.type == "export_clause":
                for spec in child.children:
                    if spec.type == "export_specifier":
                        identifiers = [c for c in spec.children if c.type == "identifier"]
                        if identifiers:
                            name = identifiers[0].text.decode("utf-8")
                            exports.append(Export(name=name, line=line, kind="variable"))
            elif child.type == "identifier" and is_default:
                exports.append(Export(
                    name=child.text.decode("utf-8"),
                    line=line,
                    kind="default",
                ))
        return exports

    # ------------------------------------------------------------------
    # Functions
    # ------------------------------------------------------------------

    def _extract_functions(self, root: Node) -> list[Function]:
        functions: list[Function] = []
        for child in root.children:
            self._collect_functions(child, functions)
        return functions

    def _collect_functions(self, node: Node, functions: list[Function]) -> None:
        if node.type == "function_declaration":
            name_node = node.child_by_field_name("name")
            if name_node:
                functions.append(self._make_function(name_node.text.decode("utf-8"), node))
        elif node.type in ("lexical_declaration", "variable_declaration"):
            for child in node.children:
                if child.type == "variable_declarator":
                    name_node = child.child_by_field_name("name")
                    value_node = child.child_by_field_name("value")
                    if name_node and value_node and value_node.type in ("arrow_function", "function_expression", "function"):
                        functions.append(self._make_function(
                            name_node.text.decode("utf-8"), value_node,
                        ))
        elif node.type == "export_statement":
            for child in node.children:
                self._collect_functions(child, functions)
        elif node.type == "class_declaration":
            body = node.child_by_field_name("body")
            if body:
                for child in body.children:
                    if child.type == "method_definition":
                        name_node = child.child_by_field_name("name")
                        if name_node:
                            functions.append(self._make_function(
                                name_node.text.decode("utf-8"), child,
                            ))

    def _make_function(self, name: str, node: Node) -> Function:
        line = node.start_point[0] + 1
        end_line = node.end_point[0] + 1
        param_count = self._count_params(node)
        return Function(
            name=name,
            line=line,
            end_line=end_line,
            param_count=param_count,
            line_count=end_line - line + 1,
        )

    def _count_params(self, node: Node) -> int:
        params_node = node.child_by_field_name("parameters")
        if params_node is None:
            # For arrow functions, the parameter might be a single identifier
            # or a formal_parameters node
            for child in node.children:
                if child.type == "formal_parameters":
                    params_node = child
                    break
        if params_node is None:
            return 0
        count = 0
        for child in params_node.children:
            if child.type in (
                "identifier",
                "required_parameter",
                "optional_parameter",
                "rest_parameter",
                "assignment_pattern",
                "object_pattern",
                "array_pattern",
            ):
                count += 1
        return count

    # ------------------------------------------------------------------
    # Classes
    # ------------------------------------------------------------------

    def _extract_classes(self, root: Node) -> list[Class]:
        classes: list[Class] = []
        for child in root.children:
            self._collect_classes(child, classes)
        return classes

    def _collect_classes(self, node: Node, classes: list[Class]) -> None:
        if node.type == "class_declaration":
            name_node = node.child_by_field_name("name")
            if name_node:
                line = node.start_point[0] + 1
                end_line = node.end_point[0] + 1
                body = node.child_by_field_name("body")
                method_count = 0
                if body:
                    method_count = sum(
                        1 for c in body.children if c.type == "method_definition"
                    )
                classes.append(Class(
                    name=name_node.text.decode("utf-8"),
                    line=line,
                    end_line=end_line,
                    method_count=method_count,
                    line_count=end_line - line + 1,
                ))
        elif node.type == "export_statement":
            for child in node.children:
                self._collect_classes(child, classes)

    # ------------------------------------------------------------------
    # Variables (module-level only)
    # ------------------------------------------------------------------

    def _extract_variables(self, root: Node) -> list[Variable]:
        variables: list[Variable] = []
        for child in root.children:
            self._collect_variables(child, variables)
        return variables

    def _collect_variables(self, node: Node, variables: list[Variable]) -> None:
        if node.type in ("lexical_declaration", "variable_declaration"):
            for child in node.children:
                if child.type == "variable_declarator":
                    name_node = child.child_by_field_name("name")
                    value_node = child.child_by_field_name("value")
                    # Skip if it's a function/arrow/class expression (those are functions, not variables)
                    if value_node and value_node.type in ("arrow_function", "function_expression", "function", "class"):
                        continue
                    if name_node:
                        variables.append(Variable(
                            name=name_node.text.decode("utf-8"),
                            line=node.start_point[0] + 1,
                            scope="module",
                        ))
        elif node.type == "export_statement":
            for child in node.children:
                self._collect_variables(child, variables)

    # ------------------------------------------------------------------
    # Calls (walk full tree)
    # ------------------------------------------------------------------

    def _extract_calls(self, root: Node) -> list[Call]:
        calls: list[Call] = []
        self._walk_calls(root, calls)
        return calls

    def _walk_calls(self, node: Node, calls: list[Call]) -> None:
        if node.type == "call_expression":
            func_node = node.child_by_field_name("function")
            if func_node:
                name = self._call_name(func_node)
                if name:
                    calls.append(Call(name=name, line=node.start_point[0] + 1))
        for child in node.children:
            self._walk_calls(child, calls)

    def _call_name(self, node: Node) -> str | None:
        if node.type == "identifier":
            return node.text.decode("utf-8")
        if node.type == "member_expression":
            obj = node.child_by_field_name("object")
            prop = node.child_by_field_name("property")
            if obj and prop:
                obj_name = self._call_name(obj)
                prop_name = prop.text.decode("utf-8")
                if obj_name:
                    return f"{obj_name}.{prop_name}"
                return prop_name
        return None
