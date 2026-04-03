from __future__ import annotations

import re
from pathlib import Path

from .base import Parser
from ..finding import Call, Class, Export, Function, Import, ParsedFile, Variable

try:
    import tree_sitter_rust as tsrust
    from tree_sitter import Language, Parser as TSParser
    RUST_LANGUAGE = Language(tsrust.language())
except ImportError:
    RUST_LANGUAGE = None


class RustParser(Parser):
    """Parse Rust files using tree-sitter-rust with regex fallback."""

    def supports(self, path: Path) -> bool:
        return path.suffix == ".rs"

    def parse_file(self, path: Path) -> ParsedFile:
        source = path.read_text(encoding="utf-8", errors="replace")
        raw_lines = source.count("\n") + (1 if source and not source.endswith("\n") else 0)

        try:
            if RUST_LANGUAGE is not None:
                return self._parse_tree_sitter(path, source, raw_lines)
            return self._parse_regex(path, source, raw_lines)
        except Exception:
            return ParsedFile(path=path, raw_lines=raw_lines)

    # ── tree-sitter ──────────────────────────────────────────────

    def _parse_tree_sitter(self, path: Path, source: str, raw_lines: int) -> ParsedFile:
        parser = TSParser(RUST_LANGUAGE)
        tree = parser.parse(source.encode("utf-8"))
        root = tree.root_node

        imports = self._ts_imports(root)
        functions = self._ts_functions(root)
        classes = self._ts_classes(root, source, functions)
        variables = self._ts_variables(root)
        calls = self._ts_calls(root)
        exports = self._ts_exports(root, functions, classes, variables)

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
            if node.type == "use_declaration":
                text = node.text.decode("utf-8")
                # Strip `use ` prefix and `;` suffix
                path_str = text.lstrip("use").strip().rstrip(";").strip()
                # Handle `use std::collections::HashMap;`
                parts = path_str.split("::")
                name = parts[-1] if parts else path_str
                # Handle glob `use std::io::*;`
                if name == "*":
                    name = parts[-2] if len(parts) > 1 else "*"
                # Handle group imports `use std::{io, fs};` - split into multiple
                if "{" in name:
                    base = "::".join(parts[:-1])
                    inner = name.strip("{}").strip()
                    for item in inner.split(","):
                        item = item.strip()
                        if item:
                            # Handle `self` in group imports
                            imp_name = item.split("::")[0].strip()
                            imports.append(Import(
                                name=imp_name,
                                source=f"{base}::{imp_name}" if base else imp_name,
                                line=node.start_point[0] + 1,
                            ))
                else:
                    imports.append(Import(
                        name=name,
                        source=path_str,
                        line=node.start_point[0] + 1,
                    ))
        return imports

    def _ts_functions(self, root) -> list[Function]:
        functions: list[Function] = []
        for node in root.children:
            if node.type == "function_item":
                functions.append(self._ts_make_function(node))
            elif node.type == "impl_item":
                # Collect methods inside impl blocks
                impl_type = self._ts_impl_type(node)
                body = node.child_by_field_name("body")
                if body:
                    for child in body.children:
                        if child.type == "function_item":
                            fn = self._ts_make_function(child, impl_type)
                            functions.append(fn)
        return functions

    def _ts_make_function(self, node, impl_type: str | None = None) -> Function:
        name_node = node.child_by_field_name("name")
        name = name_node.text.decode("utf-8") if name_node else "<anonymous>"
        if impl_type:
            name = f"{impl_type}.{name}"
        line = node.start_point[0] + 1
        end_line = node.end_point[0] + 1
        param_count = self._ts_param_count(node)
        return Function(
            name=name,
            line=line,
            end_line=end_line,
            param_count=param_count,
            line_count=end_line - line + 1,
        )

    def _ts_param_count(self, node) -> int:
        params = node.child_by_field_name("parameters")
        if not params:
            return 0
        count = 0
        for child in params.children:
            if child.type in ("parameter", "self_parameter"):
                count += 1
        return count

    def _ts_impl_type(self, node) -> str | None:
        """Extract the type name from an impl block."""
        type_node = node.child_by_field_name("type")
        if type_node:
            return type_node.text.decode("utf-8")
        return None

    def _ts_classes(self, root, source: str, functions: list[Function]) -> list[Class]:
        classes: list[Class] = []
        for node in root.children:
            if node.type in ("struct_item", "enum_item", "trait_item"):
                name_node = node.child_by_field_name("name")
                if name_node:
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

    def _ts_variables(self, root) -> list[Variable]:
        variables: list[Variable] = []
        for node in root.children:
            if node.type in ("static_item", "const_item"):
                name_node = node.child_by_field_name("name")
                if name_node:
                    variables.append(Variable(
                        name=name_node.text.decode("utf-8"),
                        line=node.start_point[0] + 1,
                        scope="module",
                    ))
        return variables

    def _ts_calls(self, root) -> list[Call]:
        calls: list[Call] = []
        for node in self._walk(root):
            if node.type == "call_expression":
                func_node = node.child_by_field_name("function")
                if func_node:
                    name = func_node.text.decode("utf-8")
                    calls.append(Call(name=name, line=node.start_point[0] + 1))
        return calls

    def _ts_exports(
        self,
        root,
        functions: list[Function],
        classes: list[Class],
        variables: list[Variable],
    ) -> list[Export]:
        """Items marked `pub` are exported."""
        exports: list[Export] = []
        pub_names: set[str] = set()

        for node in root.children:
            if node.type == "function_item":
                if self._is_pub(node):
                    name_node = node.child_by_field_name("name")
                    if name_node:
                        pub_names.add(name_node.text.decode("utf-8"))
            elif node.type in ("struct_item", "enum_item", "trait_item"):
                if self._is_pub(node):
                    name_node = node.child_by_field_name("name")
                    if name_node:
                        pub_names.add(name_node.text.decode("utf-8"))
            elif node.type in ("static_item", "const_item"):
                if self._is_pub(node):
                    name_node = node.child_by_field_name("name")
                    if name_node:
                        pub_names.add(name_node.text.decode("utf-8"))
            elif node.type == "impl_item":
                body = node.child_by_field_name("body")
                if body:
                    for child in body.children:
                        if child.type == "function_item" and self._is_pub(child):
                            name_node = child.child_by_field_name("name")
                            if name_node:
                                impl_type = self._ts_impl_type(node)
                                full = f"{impl_type}.{name_node.text.decode('utf-8')}" if impl_type else name_node.text.decode("utf-8")
                                pub_names.add(full)

        for fn in functions:
            short = fn.name.rsplit(".", 1)[-1]
            if fn.name in pub_names or short in pub_names:
                exports.append(Export(name=fn.name, line=fn.line, kind="function"))
        for cls in classes:
            if cls.name in pub_names:
                exports.append(Export(name=cls.name, line=cls.line, kind="class"))
        for var in variables:
            if var.name in pub_names:
                exports.append(Export(name=var.name, line=var.line, kind="variable"))
        return exports

    def _is_pub(self, node) -> bool:
        """Check if a node has a `pub` visibility modifier."""
        for child in node.children:
            if child.type == "visibility_modifier":
                return True
        return False

    def _walk(self, node):
        """Depth-first walk of all tree-sitter nodes."""
        yield node
        for child in node.children:
            yield from self._walk(child)

    # ── regex fallback ───────────────────────────────────────────

    def _parse_regex(self, path: Path, source: str, raw_lines: int) -> ParsedFile:
        lines = source.splitlines()

        imports = self._re_imports(lines)
        functions, impl_map = self._re_functions(lines)
        classes = self._re_classes(lines, functions)
        variables = self._re_variables(lines)
        calls = self._re_calls(lines)
        exports = self._re_exports(lines, functions, classes, variables)

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
        _use = re.compile(r'^\s*use\s+(.+);')

        for i, line in enumerate(lines):
            m = _use.match(line)
            if m:
                path_str = m.group(1).strip()
                # Handle group imports: `use std::{io, fs};`
                group_match = re.match(r'(.+)::\{(.+)\}', path_str)
                if group_match:
                    base = group_match.group(1)
                    items = group_match.group(2)
                    for item in items.split(","):
                        item = item.strip()
                        if item:
                            imp_name = item.split("::")[0].split(" as ")[0].strip()
                            alias = None
                            if " as " in item:
                                alias = item.split(" as ")[1].strip()
                            imports.append(Import(
                                name=imp_name,
                                source=f"{base}::{imp_name}",
                                line=i + 1,
                                alias=alias,
                            ))
                else:
                    parts = path_str.split("::")
                    name = parts[-1].strip()
                    alias = None
                    if " as " in name:
                        name, alias = name.split(" as ", 1)
                        name = name.strip()
                        alias = alias.strip()
                    if name == "*":
                        name = parts[-2] if len(parts) > 1 else "*"
                    imports.append(Import(
                        name=name,
                        source=path_str.split(" as ")[0].strip(),
                        line=i + 1,
                        alias=alias,
                    ))
        return imports

    def _re_functions(self, lines: list[str]) -> tuple[list[Function], dict[str, str]]:
        functions: list[Function] = []
        impl_map: dict[str, str] = {}  # line -> impl type

        _impl = re.compile(r'^\s*impl(?:<[^>]*>)?\s+(\w+)')
        _fn = re.compile(
            r'^\s*(?:pub(?:\s*\([^)]*\))?\s+)?(?:async\s+)?(?:unsafe\s+)?(?:extern\s+"[^"]*"\s+)?'
            r'fn\s+(\w+)\s*(?:<[^>]*>)?\s*\(([^)]*)\)'
        )

        current_impl: str | None = None
        impl_depth = 0

        for i, line in enumerate(lines):
            stripped = line.strip()

            # Track impl blocks
            m_impl = _impl.match(stripped)
            if m_impl and "{" in stripped:
                current_impl = m_impl.group(1)
                impl_depth = 1
                for ch in stripped[stripped.index("{") + 1:]:
                    if ch == "{":
                        impl_depth += 1
                    elif ch == "}":
                        impl_depth -= 1
                if impl_depth <= 0:
                    current_impl = None
                continue

            if current_impl is not None:
                for ch in stripped:
                    if ch == "{":
                        impl_depth += 1
                    elif ch == "}":
                        impl_depth -= 1
                if impl_depth <= 0:
                    current_impl = None

            m_fn = _fn.match(stripped)
            if m_fn:
                name = m_fn.group(1)
                params_str = m_fn.group(2)
                if current_impl:
                    full_name = f"{current_impl}.{name}"
                else:
                    full_name = name
                param_count = self._count_params(params_str)
                end_line = self._find_block_end(lines, i)
                functions.append(Function(
                    name=full_name,
                    line=i + 1,
                    end_line=end_line + 1,
                    param_count=param_count,
                    line_count=end_line - i + 1,
                ))
        return functions, impl_map

    def _count_params(self, params_str: str) -> int:
        params_str = params_str.strip()
        if not params_str:
            return 0
        # Handle nested generics by removing them
        cleaned = re.sub(r'<[^>]*>', '', params_str)
        return len([p for p in cleaned.split(",") if p.strip()])

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

    def _re_classes(self, lines: list[str], functions: list[Function]) -> list[Class]:
        classes: list[Class] = []
        _struct = re.compile(r'^\s*(?:pub(?:\s*\([^)]*\))?\s+)?struct\s+(\w+)')
        _enum = re.compile(r'^\s*(?:pub(?:\s*\([^)]*\))?\s+)?enum\s+(\w+)')
        _trait = re.compile(r'^\s*(?:pub(?:\s*\([^)]*\))?\s+)?trait\s+(\w+)')

        for i, line in enumerate(lines):
            stripped = line.strip()
            for pattern in (_struct, _enum, _trait):
                m = pattern.match(stripped)
                if m:
                    name = m.group(1)
                    end_line = self._find_block_end(lines, i)
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
                    break
        return classes

    def _re_variables(self, lines: list[str]) -> list[Variable]:
        variables: list[Variable] = []
        _static = re.compile(r'^\s*(?:pub(?:\s*\([^)]*\))?\s+)?static\s+(?:mut\s+)?(\w+)\s*:')
        _const = re.compile(r'^\s*(?:pub(?:\s*\([^)]*\))?\s+)?const\s+(\w+)\s*:')

        for i, line in enumerate(lines):
            for pattern in (_static, _const):
                m = pattern.match(line)
                if m:
                    variables.append(Variable(
                        name=m.group(1), line=i + 1, scope="module",
                    ))
                    break
        return variables

    def _re_calls(self, lines: list[str]) -> list[Call]:
        calls: list[Call] = []
        _call = re.compile(r'([\w:.]+)\s*[!]?\s*\(')
        _keywords = {
            "fn", "if", "for", "while", "match", "loop", "return",
            "let", "mut", "pub", "use", "mod", "struct", "enum",
            "trait", "impl", "async", "await", "unsafe", "where",
        }
        for i, line in enumerate(lines):
            for m in _call.finditer(line):
                name = m.group(1)
                if name not in _keywords and not name.startswith("//"):
                    calls.append(Call(name=name, line=i + 1))
        return calls

    def _re_exports(
        self,
        lines: list[str],
        functions: list[Function],
        classes: list[Class],
        variables: list[Variable],
    ) -> list[Export]:
        """Scan for `pub` items in the source."""
        exports: list[Export] = []
        pub_fns: set[str] = set()
        pub_types: set[str] = set()
        pub_vars: set[str] = set()

        _pub_fn = re.compile(
            r'^\s*pub(?:\s*\([^)]*\))?\s+(?:async\s+)?(?:unsafe\s+)?fn\s+(\w+)'
        )
        _pub_struct = re.compile(r'^\s*pub(?:\s*\([^)]*\))?\s+struct\s+(\w+)')
        _pub_enum = re.compile(r'^\s*pub(?:\s*\([^)]*\))?\s+enum\s+(\w+)')
        _pub_trait = re.compile(r'^\s*pub(?:\s*\([^)]*\))?\s+trait\s+(\w+)')
        _pub_static = re.compile(r'^\s*pub(?:\s*\([^)]*\))?\s+static\s+(?:mut\s+)?(\w+)')
        _pub_const = re.compile(r'^\s*pub(?:\s*\([^)]*\))?\s+const\s+(\w+)')

        for line in lines:
            for pat in (_pub_fn,):
                m = pat.match(line)
                if m:
                    pub_fns.add(m.group(1))
            for pat in (_pub_struct, _pub_enum, _pub_trait):
                m = pat.match(line)
                if m:
                    pub_types.add(m.group(1))
            for pat in (_pub_static, _pub_const):
                m = pat.match(line)
                if m:
                    pub_vars.add(m.group(1))

        for fn in functions:
            short = fn.name.rsplit(".", 1)[-1]
            if short in pub_fns:
                exports.append(Export(name=fn.name, line=fn.line, kind="function"))
        for cls in classes:
            if cls.name in pub_types:
                exports.append(Export(name=cls.name, line=cls.line, kind="class"))
        for var in variables:
            if var.name in pub_vars:
                exports.append(Export(name=var.name, line=var.line, kind="variable"))
        return exports
