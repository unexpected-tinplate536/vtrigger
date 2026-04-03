from __future__ import annotations

import ast
from pathlib import Path

from .base import Parser
from ..finding import Call, Class, Export, Function, Import, ParsedFile, Variable


class PythonParser(Parser):
    """Parse Python files using the stdlib ast module."""

    def supports(self, path: Path) -> bool:
        return path.suffix == ".py"

    def parse_file(self, path: Path) -> ParsedFile:
        source = path.read_text(encoding="utf-8", errors="replace")
        raw_lines = source.count("\n") + (1 if source and not source.endswith("\n") else 0)

        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError:
            return ParsedFile(path=path, raw_lines=raw_lines)

        imports = self._extract_imports(tree)
        functions = self._extract_functions(tree)
        classes = self._extract_classes(tree)
        variables = self._extract_variables(tree)
        exports = self._extract_exports(functions, classes, variables)
        calls = self._extract_calls(tree)

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

    def _extract_imports(self, tree: ast.Module) -> list[Import]:
        imports: list[Import] = []
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.append(Import(
                        name=alias.name,
                        source=None,
                        line=node.lineno,
                        alias=alias.asname,
                    ))
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for alias in node.names:
                    imports.append(Import(
                        name=alias.name,
                        source=module,
                        line=node.lineno,
                        alias=alias.asname,
                    ))
        return imports

    def _extract_functions(self, tree: ast.Module) -> list[Function]:
        functions: list[Function] = []
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                functions.append(self._make_function(node))
            elif isinstance(node, (ast.ClassDef,)):
                for child in ast.iter_child_nodes(node):
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        functions.append(self._make_function(child))
        return functions

    def _make_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> Function:
        end_line = node.end_lineno or node.lineno
        args = node.args
        param_count = (
            len(args.args)
            + len(args.posonlyargs)
            + len(args.kwonlyargs)
            + (1 if args.vararg else 0)
            + (1 if args.kwarg else 0)
        )
        return Function(
            name=node.name,
            line=node.lineno,
            end_line=end_line,
            param_count=param_count,
            line_count=end_line - node.lineno + 1,
        )

    def _extract_classes(self, tree: ast.Module) -> list[Class]:
        classes: list[Class] = []
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.ClassDef):
                end_line = node.end_lineno or node.lineno
                method_count = sum(
                    1
                    for child in ast.iter_child_nodes(node)
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                )
                classes.append(Class(
                    name=node.name,
                    line=node.lineno,
                    end_line=end_line,
                    method_count=method_count,
                    line_count=end_line - node.lineno + 1,
                ))
        return classes

    def _extract_variables(self, tree: ast.Module) -> list[Variable]:
        variables: list[Variable] = []
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    for name in self._target_names(target):
                        variables.append(Variable(name=name, line=node.lineno, scope="module"))
            elif isinstance(node, ast.AnnAssign) and node.target:
                for name in self._target_names(node.target):
                    variables.append(Variable(name=name, line=node.lineno, scope="module"))
            elif isinstance(node, ast.AugAssign):
                for name in self._target_names(node.target):
                    variables.append(Variable(name=name, line=node.lineno, scope="module"))
        return variables

    def _target_names(self, node: ast.expr) -> list[str]:
        if isinstance(node, ast.Name):
            return [node.id]
        if isinstance(node, ast.Tuple):
            names: list[str] = []
            for elt in node.elts:
                names.extend(self._target_names(elt))
            return names
        return []

    def _extract_exports(
        self,
        functions: list[Function],
        classes: list[Class],
        variables: list[Variable],
    ) -> list[Export]:
        exports: list[Export] = []
        for fn in functions:
            if not fn.name.startswith("_"):
                exports.append(Export(name=fn.name, line=fn.line, kind="function"))
        for cls in classes:
            if not cls.name.startswith("_"):
                exports.append(Export(name=cls.name, line=cls.line, kind="class"))
        for var in variables:
            if not var.name.startswith("_"):
                exports.append(Export(name=var.name, line=var.line, kind="variable"))
        return exports

    def _extract_calls(self, tree: ast.Module) -> list[Call]:
        calls: list[Call] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = self._call_name(node.func)
                if name:
                    calls.append(Call(name=name, line=node.lineno))
        return calls

    def _call_name(self, node: ast.expr) -> str | None:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            value = self._call_name(node.value)
            if value:
                return f"{value}.{node.attr}"
            return node.attr
        return None
