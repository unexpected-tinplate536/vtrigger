from __future__ import annotations

import re
from pathlib import Path

from .base import Parser
from ..finding import Call, Class, Export, Function, Import, ParsedFile, Variable

try:
    import tree_sitter_c as tsc
    from tree_sitter import Language, Parser as TSParser
    C_LANGUAGE = Language(tsc.language())
except ImportError:
    C_LANGUAGE = None

try:
    import tree_sitter_cpp as tscpp
    CPP_LANGUAGE = Language(tscpp.language())
except ImportError:
    CPP_LANGUAGE = None


_CPP_SUFFIXES = {".cpp", ".cc", ".cxx", ".hpp", ".hxx"}
_C_SUFFIXES = {".c", ".h"}
_ALL_SUFFIXES = _C_SUFFIXES | _CPP_SUFFIXES


class CppParser(Parser):
    """Parse C/C++ files using tree-sitter with regex fallback."""

    def supports(self, path: Path) -> bool:
        return path.suffix in _ALL_SUFFIXES

    def parse_file(self, path: Path) -> ParsedFile:
        source = path.read_text(encoding="utf-8", errors="replace")
        raw_lines = source.count("\n") + (1 if source and not source.endswith("\n") else 0)
        is_cpp = path.suffix in _CPP_SUFFIXES

        try:
            lang = None
            if is_cpp and CPP_LANGUAGE is not None:
                lang = CPP_LANGUAGE
            elif not is_cpp and C_LANGUAGE is not None:
                lang = C_LANGUAGE
            elif CPP_LANGUAGE is not None:
                # .h files: try C++ parser as fallback
                lang = CPP_LANGUAGE

            if lang is not None:
                return self._parse_tree_sitter(path, source, raw_lines, lang)
            return self._parse_regex(path, source, raw_lines)
        except Exception:
            return ParsedFile(path=path, raw_lines=raw_lines)

    # ── tree-sitter ──────────────────────────────────────────────

    def _parse_tree_sitter(self, path: Path, source: str, raw_lines: int, lang) -> ParsedFile:
        parser = TSParser(lang)
        tree = parser.parse(source.encode("utf-8"))
        root = tree.root_node

        imports = self._ts_imports(root)
        functions = self._ts_functions(root)
        classes = self._ts_classes(root)
        variables = self._ts_variables(root)
        calls = self._ts_calls(root)
        exports = self._build_exports(functions, classes, variables, root)

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
            if node.type == "preproc_include":
                path_node = node.child_by_field_name("path")
                if path_node:
                    inc_path = path_node.text.decode("utf-8").strip('"<>')
                    imports.append(Import(
                        name=inc_path,
                        source=inc_path,
                        line=node.start_point[0] + 1,
                    ))
            elif node.type == "using_declaration":
                text = node.text.decode("utf-8").strip().rstrip(";")
                # using std::vector;
                name = text.replace("using ", "").strip()
                imports.append(Import(
                    name=name,
                    source=name,
                    line=node.start_point[0] + 1,
                ))
            elif node.type == "namespace_definition" and False:
                pass  # skip namespace defs
            elif node.type == "using_declaration" or (
                node.type == "expression_statement" and
                node.text and node.text.decode("utf-8").strip().startswith("using namespace")
            ):
                pass  # handled above
        # Also catch "using namespace X;" which tree-sitter may parse differently
        for node in root.children:
            text = node.text.decode("utf-8").strip() if node.text else ""
            if text.startswith("using namespace "):
                ns = text.replace("using namespace ", "").rstrip(";").strip()
                imports.append(Import(
                    name=ns,
                    source=ns,
                    line=node.start_point[0] + 1,
                ))
        return imports

    def _ts_functions(self, root) -> list[Function]:
        functions: list[Function] = []
        for node in self._walk(root):
            if node.type == "function_definition":
                declarator = node.child_by_field_name("declarator")
                name = self._extract_function_name(declarator)
                if name:
                    line = node.start_point[0] + 1
                    end_line = node.end_point[0] + 1
                    param_count = self._ts_param_count(declarator)
                    functions.append(Function(
                        name=name,
                        line=line,
                        end_line=end_line,
                        param_count=param_count,
                        line_count=end_line - line + 1,
                    ))
        return functions

    def _extract_function_name(self, declarator) -> str | None:
        """Extract function name from a declarator node, handling nested forms."""
        if declarator is None:
            return None
        if declarator.type == "function_declarator":
            inner = declarator.child_by_field_name("declarator")
            if inner:
                if inner.type == "qualified_identifier" or inner.type == "scoped_identifier":
                    return inner.text.decode("utf-8")
                if inner.type == "identifier":
                    return inner.text.decode("utf-8")
                if inner.type == "field_identifier":
                    return inner.text.decode("utf-8")
                # Parenthesized or pointer declarator
                return self._extract_function_name(inner)
        if declarator.type == "pointer_declarator":
            return self._extract_function_name(
                declarator.child_by_field_name("declarator")
            )
        if declarator.type == "identifier":
            return declarator.text.decode("utf-8")
        return None

    def _ts_param_count(self, declarator) -> int:
        """Count parameters in a function declarator."""
        if declarator is None:
            return 0
        if declarator.type == "function_declarator":
            params = declarator.child_by_field_name("parameters")
            if params:
                count = 0
                for child in params.children:
                    if child.type in ("parameter_declaration", "optional_parameter_declaration",
                                      "variadic_parameter_declaration"):
                        count += 1
                return count
        # Recurse for pointer/reference declarators
        for child in declarator.children:
            if child.type == "function_declarator":
                return self._ts_param_count(child)
        return 0

    def _ts_classes(self, root) -> list[Class]:
        classes: list[Class] = []
        for node in self._walk(root):
            if node.type in ("class_specifier", "struct_specifier"):
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
        for child in self._walk(body):
            if child.type == "function_definition":
                count += 1
            elif child.type == "declaration":
                # Check if it's a method declaration (has function_declarator)
                for sub in child.children:
                    if sub.type == "function_declarator":
                        count += 1
                        break
        return count

    def _ts_variables(self, root) -> list[Variable]:
        variables: list[Variable] = []
        for node in root.children:
            if node.type == "declaration":
                # Skip if it contains a function declarator (it's a function prototype)
                has_func = False
                for child in self._walk(node):
                    if child.type == "function_declarator":
                        has_func = True
                        break
                if has_func:
                    continue
                for child in node.children:
                    if child.type == "init_declarator":
                        decl = child.child_by_field_name("declarator")
                        if decl and decl.type == "identifier":
                            variables.append(Variable(
                                name=decl.text.decode("utf-8"),
                                line=node.start_point[0] + 1,
                                scope="module",
                            ))
                    elif child.type == "identifier":
                        # Simple declaration like `int x;`
                        # But avoid type names; the last identifier before ; is the var
                        pass
                # Fallback: find variable declarators
                for child in self._walk(node):
                    if child.type in ("identifier",) and child.parent and child.parent.type == "init_declarator":
                        if child == child.parent.child_by_field_name("declarator"):
                            # Already handled above
                            pass
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

    def _walk(self, node):
        """Depth-first walk of all tree-sitter nodes."""
        yield node
        for child in node.children:
            yield from self._walk(child)

    def _is_static_node(self, node) -> bool:
        """Check if a declaration has 'static' storage class."""
        text = node.text.decode("utf-8") if node.text else ""
        # Check for static keyword in storage class specifiers
        for child in node.children:
            if child.type == "storage_class_specifier" and child.text.decode("utf-8") == "static":
                return True
        return False

    # ── regex fallback ───────────────────────────────────────────

    def _parse_regex(self, path: Path, source: str, raw_lines: int) -> ParsedFile:
        lines = source.splitlines()
        clean_lines = self._strip_comments(lines)

        imports = self._re_imports(lines)  # use original lines for preprocessor directives
        functions = self._re_functions(clean_lines)
        classes = self._re_classes(clean_lines, functions)
        variables = self._re_variables(clean_lines)
        calls = self._re_calls(clean_lines)
        exports = self._build_exports_regex(functions, classes, variables, clean_lines)

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
                # Skip string literals
                if line[i] == '"':
                    end = i + 1
                    while end < len(line) and line[end] != '"':
                        if line[end] == '\\':
                            end += 1
                        end += 1
                    out.append(line[i:end + 1])
                    i = end + 1
                    continue
                if line[i] == "'":
                    end = i + 1
                    while end < len(line) and line[end] != "'":
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
        _include = re.compile(r'^\s*#\s*include\s+([<"])([^>"]+)[>"]')
        _using_ns = re.compile(r'^\s*using\s+namespace\s+([\w:]+)\s*;')
        _using = re.compile(r'^\s*using\s+([\w:]+)\s*;')

        for i, line in enumerate(lines):
            m = _include.match(line)
            if m:
                inc_path = m.group(2)
                imports.append(Import(
                    name=inc_path,
                    source=inc_path,
                    line=i + 1,
                ))
                continue
            m = _using_ns.match(line)
            if m:
                ns = m.group(1)
                imports.append(Import(
                    name=ns,
                    source=ns,
                    line=i + 1,
                ))
                continue
            m = _using.match(line)
            if m:
                name = m.group(1)
                imports.append(Import(
                    name=name,
                    source=name,
                    line=i + 1,
                ))
        return imports

    def _re_functions(self, lines: list[str]) -> list[Function]:
        functions: list[Function] = []
        # Match function definitions: optional qualifiers, return type, name (possibly Class::method), params, opening brace
        _func = re.compile(
            r'^\s*'
            r'(?:(?:static|inline|virtual|explicit|constexpr|extern|friend)\s+)*'
            r'(?:[\w:*&<>,\s]+?)\s+'  # return type (non-greedy)
            r'(~?(?:\w+::)*\w+)\s*'  # function name (may include Class:: or ~dtor)
            r'\(([^)]*)\)'  # params
            r'(?:\s*(?:const|override|final|noexcept|->[\w\s:&*<>]+))*'  # trailing qualifiers
            r'\s*\{'  # opening brace
        )
        _keywords = {
            "if", "for", "while", "switch", "catch", "return", "else",
            "do", "sizeof", "typeof", "alignof", "decltype", "static_assert",
        }
        for i, line in enumerate(lines):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            m = _func.match(line)
            if m:
                name = m.group(1)
                params = m.group(2)
                base_name = name.split("::")[-1].lstrip("~")
                if base_name in _keywords:
                    continue
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
            r'^\s*(?:template\s*<[^>]*>\s*)?'
            r'(?:class|struct)\s+'
            r'(?:__\w+\s+)?'  # optional attributes like __declspec
            r'(\w+)'
            r'(?:\s*(?:final\s*)?'
            r'(?::\s*(?:public|private|protected)?\s*[\w:<>,\s]+)?)?'
            r'\s*\{'
        )
        for i, line in enumerate(lines):
            m = _class.match(line)
            if m:
                name = m.group(1)
                end_line = self._find_block_end(lines, i)
                # Count methods within the class range
                method_count = sum(
                    1 for fn in functions
                    if (i + 1) <= fn.line <= (end_line + 1)
                )
                # Also count out-of-class methods (ClassName::method)
                method_count += sum(
                    1 for fn in functions
                    if fn.name.startswith(f"{name}::") and not ((i + 1) <= fn.line <= (end_line + 1))
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
        _var = re.compile(
            r'^\s*'
            r'(?:(?:static|extern|const|constexpr|volatile|thread_local|mutable)\s+)*'
            r'(?:[\w:*&<>,\s]+?)\s+'
            r'(\w+)\s*[;=]'
        )
        _keywords = {
            "if", "for", "while", "switch", "catch", "return", "else", "do",
            "class", "struct", "enum", "union", "namespace", "template",
            "typedef", "using", "include", "define", "pragma",
        }
        # Only match at file scope (no leading indentation deeper than expected)
        for i, line in enumerate(lines):
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or stripped.startswith("//"):
                continue
            # Rough heuristic: global/namespace scope lines have 0 indent or namespace-level indent
            indent = len(line) - len(line.lstrip())
            if indent > 4:
                continue  # likely inside a function body
            m = _var.match(line)
            if m:
                name = m.group(1)
                if name in _keywords:
                    continue
                # Make sure this isn't a function definition (no opening brace after)
                if "{" in line and "=" not in line.split("{")[0]:
                    continue
                variables.append(Variable(
                    name=name,
                    line=i + 1,
                    scope="module",
                ))
        return variables

    def _re_calls(self, lines: list[str]) -> list[Call]:
        calls: list[Call] = []
        _call = re.compile(r'((?:[\w]+(?:::|\.|->))*[\w]+)\s*\(')
        _keywords = {
            "if", "for", "while", "switch", "catch", "return", "else", "do",
            "sizeof", "typeof", "alignof", "decltype", "static_assert",
            "class", "struct", "enum", "union", "namespace", "template",
            "typedef", "using", "define", "include",
        }
        for i, line in enumerate(lines):
            for m in _call.finditer(line):
                name = m.group(1)
                # Get the base name (after last :: or . or ->)
                base = re.split(r'::|\.|->', name)[-1]
                if base in _keywords:
                    continue
                calls.append(Call(name=name, line=i + 1))
        return calls

    # ── shared helpers ───────────────────────────────────────────

    def _count_params(self, params_str: str) -> int:
        params_str = params_str.strip()
        if not params_str or params_str == "void":
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
        root,
    ) -> list[Export]:
        """In C/C++, non-static functions/classes are exports."""
        exports: list[Export] = []
        # Build set of static function names
        static_names: set[str] = set()
        for node in root.children:
            if node.type == "function_definition":
                if self._is_static_node(node):
                    declarator = node.child_by_field_name("declarator")
                    name = self._extract_function_name(declarator)
                    if name:
                        static_names.add(name)
            elif node.type == "declaration":
                if self._is_static_node(node):
                    for child in self._walk(node):
                        if child.type == "identifier" and child.parent and \
                           child.parent.type == "init_declarator":
                            static_names.add(child.text.decode("utf-8"))

        for fn in functions:
            if fn.name not in static_names:
                exports.append(Export(name=fn.name, line=fn.line, kind="function"))
        for cls in classes:
            exports.append(Export(name=cls.name, line=cls.line, kind="class"))
        for var in variables:
            if var.name not in static_names:
                exports.append(Export(name=var.name, line=var.line, kind="variable"))
        return exports

    def _build_exports_regex(
        self,
        functions: list[Function],
        classes: list[Class],
        variables: list[Variable],
        lines: list[str],
    ) -> list[Export]:
        """Regex-based export detection: non-static symbols are exports."""
        exports: list[Export] = []
        _static_line = re.compile(r'^\s*static\s+')

        for fn in functions:
            line_text = lines[fn.line - 1] if fn.line - 1 < len(lines) else ""
            if not _static_line.match(line_text):
                exports.append(Export(name=fn.name, line=fn.line, kind="function"))
        for cls in classes:
            exports.append(Export(name=cls.name, line=cls.line, kind="class"))
        for var in variables:
            line_text = lines[var.line - 1] if var.line - 1 < len(lines) else ""
            if not _static_line.match(line_text):
                exports.append(Export(name=var.name, line=var.line, kind="variable"))
        return exports
