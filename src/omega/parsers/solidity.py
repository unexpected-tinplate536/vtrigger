from __future__ import annotations

import re
from pathlib import Path

from .base import Parser as BaseParser
from ..finding import Call, Class, Export, Function, Import, ParsedFile, Variable

# Try tree-sitter first; fall back to regex parsing.
try:
    import tree_sitter_solidity as tssol
    from tree_sitter import Language
    SOL_LANGUAGE: Language | None = Language(tssol.language())
except Exception:
    SOL_LANGUAGE = None

# ---------------------------------------------------------------------------
# Regex patterns for the fallback parser
# ---------------------------------------------------------------------------

# Imports ---------------------------------------------------------------
# import "./Foo.sol";
_RE_IMPORT_PLAIN = re.compile(
    r'''import\s+["']([^"']+)["']\s*;''',
)
# import {Bar, Baz as B} from "./Foo.sol";
_RE_IMPORT_NAMED = re.compile(
    r'''import\s*\{([^}]+)\}\s*from\s*["']([^"']+)["']\s*;''',
)
# import * as X from "./Y.sol";
_RE_IMPORT_STAR = re.compile(
    r'''import\s*\*\s+as\s+(\w+)\s+from\s*["']([^"']+)["']\s*;''',
)
# import "./Foo.sol" as Foo;
_RE_IMPORT_ALIAS = re.compile(
    r'''import\s+["']([^"']+)["']\s+as\s+(\w+)\s*;''',
)

# Contracts / interfaces / libraries -----------------------------------
_RE_CONTRACT = re.compile(
    r'^(?:abstract\s+)?(?:contract|interface|library)\s+(\w+)',
    re.MULTILINE,
)

# Functions -------------------------------------------------------------
_RE_FUNCTION = re.compile(
    r'^(\s*)function\s+(\w+)\s*\(([^)]*)\)',
    re.MULTILINE,
)
# constructor, fallback, receive (no name after keyword)
_RE_SPECIAL_FUNC = re.compile(
    r'^\s*(constructor|fallback|receive)\s*\(([^)]*)\)',
    re.MULTILINE,
)

# State variables -------------------------------------------------------
# Matches common state variable declarations inside contracts:
#   mapping(...) public foo;  /  uint256 public bar;  /  address private _owner;
_RE_STATE_VAR = re.compile(
    r'^\s+(?:mapping\s*\([^)]*\)|[a-zA-Z_]\w*(?:\[\])*)\s+'
    r'(?:(?:public|private|internal|constant|immutable|override)\s+)*'
    r'(\w+)\s*[;=]',
    re.MULTILINE,
)

# Function calls --------------------------------------------------------
# Matches foo(...), bar.baz(...), emit Foo(...)
_RE_CALL = re.compile(
    r'(?:emit\s+)?(\b[a-zA-Z_]\w*(?:\.\w+)*)\s*\(',
)


class SolidityParser(BaseParser):
    """Parse Solidity .sol files.

    Uses tree-sitter-solidity when available; otherwise falls back to
    regex-based extraction which is less precise but sufficient for the
    detectors.
    """

    def supports(self, path: Path) -> bool:
        return path.suffix == ".sol"

    def parse_file(self, path: Path) -> ParsedFile:
        source = path.read_text(encoding="utf-8", errors="replace")
        raw_lines = source.count("\n") + (1 if source and not source.endswith("\n") else 0)

        if SOL_LANGUAGE is not None:
            return self._parse_tree_sitter(path, source, raw_lines)
        return self._parse_regex(path, source, raw_lines)

    # ==================================================================
    # Tree-sitter path
    # ==================================================================

    def _parse_tree_sitter(self, path: Path, source: str, raw_lines: int) -> ParsedFile:
        from tree_sitter import Parser as TSParser

        parser = TSParser(SOL_LANGUAGE)  # type: ignore[arg-type]
        try:
            tree = parser.parse(source.encode("utf-8"))
        except Exception:
            return ParsedFile(path=path, raw_lines=raw_lines)

        root = tree.root_node

        imports = self._ts_imports(root)
        functions = self._ts_functions(root)
        classes = self._ts_classes(root)
        variables = self._ts_variables(root)
        exports = self._build_exports(functions, classes)
        calls = self._ts_calls(root)

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

    # --- tree-sitter helpers ---

    def _ts_imports(self, root) -> list[Import]:  # type: ignore[type-arg]
        imports: list[Import] = []
        for node in self._ts_walk_type(root, "import_directive"):
            line = node.start_point[0] + 1
            source_node = None
            for child in node.children:
                if child.type == "string" or child.type == "import_path":
                    source_node = child
            source = source_node.text.decode("utf-8").strip("'\"") if source_node else None
            # Named imports
            names_found = False
            for child in node.children:
                if child.type == "import_clause":
                    for spec in child.children:
                        if spec.type == "import_specifier":
                            ids = [c for c in spec.children if c.type == "identifier"]
                            if ids:
                                name = ids[0].text.decode("utf-8")
                                alias = ids[1].text.decode("utf-8") if len(ids) > 1 else None
                                imports.append(Import(name=name, source=source, line=line, alias=alias))
                                names_found = True
            if not names_found and source:
                imports.append(Import(name="*", source=source, line=line))
        return imports

    def _ts_functions(self, root) -> list[Function]:
        functions: list[Function] = []
        for node in self._ts_walk_type(root, "function_definition"):
            name_node = node.child_by_field_name("name")
            name = name_node.text.decode("utf-8") if name_node else "anonymous"
            functions.append(self._ts_make_function(name, node))
        # constructor, fallback, receive
        for kind in ("constructor_definition", "fallback_receive_definition", "receive_definition"):
            for node in self._ts_walk_type(root, kind):
                functions.append(self._ts_make_function(kind.split("_")[0], node))
        return functions

    def _ts_make_function(self, name: str, node) -> Function:
        line = node.start_point[0] + 1
        end_line = node.end_point[0] + 1
        param_count = 0
        params = node.child_by_field_name("parameters")
        if params:
            param_count = sum(1 for c in params.children if c.type == "parameter")
        return Function(
            name=name,
            line=line,
            end_line=end_line,
            param_count=param_count,
            line_count=end_line - line + 1,
        )

    def _ts_classes(self, root) -> list[Class]:
        classes: list[Class] = []
        for kind in ("contract_declaration", "interface_declaration", "library_declaration"):
            for node in self._ts_walk_type(root, kind):
                name_node = node.child_by_field_name("name")
                if not name_node:
                    continue
                line = node.start_point[0] + 1
                end_line = node.end_point[0] + 1
                method_count = sum(
                    1 for c in self._ts_walk_type(node, "function_definition")
                )
                classes.append(Class(
                    name=name_node.text.decode("utf-8"),
                    line=line,
                    end_line=end_line,
                    method_count=method_count,
                    line_count=end_line - line + 1,
                ))
        return classes

    def _ts_variables(self, root) -> list[Variable]:
        variables: list[Variable] = []
        for node in self._ts_walk_type(root, "state_variable_declaration"):
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
        self._ts_walk_calls(root, calls)
        return calls

    def _ts_walk_calls(self, node, calls: list[Call]) -> None:
        if node.type in ("function_call", "call_expression"):
            func = node.child_by_field_name("function")
            if func:
                name = func.text.decode("utf-8")
                calls.append(Call(name=name, line=node.start_point[0] + 1))
        # emit statements
        if node.type == "emit_statement":
            for child in node.children:
                if child.type == "call_expression" or child.type == "function_call":
                    func = child.child_by_field_name("function")
                    name = func.text.decode("utf-8") if func else child.text.decode("utf-8")
                    calls.append(Call(name=f"emit {name}", line=node.start_point[0] + 1))
                    break
                if child.type == "identifier":
                    calls.append(Call(name=f"emit {child.text.decode('utf-8')}", line=node.start_point[0] + 1))
        for child in node.children:
            self._ts_walk_calls(child, calls)

    def _ts_walk_type(self, node, type_name: str):
        """Recursively yield all descendants of *node* with the given type."""
        if node.type == type_name:
            yield node
        for child in node.children:
            yield from self._ts_walk_type(child, type_name)

    # ==================================================================
    # Regex fallback path
    # ==================================================================

    def _parse_regex(self, path: Path, source: str, raw_lines: int) -> ParsedFile:
        # Strip single-line and multi-line comments to avoid false positives
        cleaned = self._strip_comments(source)

        imports = self._regex_imports(cleaned)
        functions = self._regex_functions(cleaned, source)
        classes = self._regex_classes(cleaned, source)
        variables = self._regex_variables(cleaned)
        exports = self._build_exports(functions, classes)
        calls = self._regex_calls(cleaned)

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

    # --- comment stripping ---

    def _strip_comments(self, source: str) -> str:
        """Remove // and /* */ comments, preserving line count."""
        # Multi-line comments first
        def _replace_ml(m: re.Match) -> str:
            return "\n" * m.group(0).count("\n")

        out = re.sub(r'/\*.*?\*/', _replace_ml, source, flags=re.DOTALL)
        # Single-line comments
        out = re.sub(r'//[^\n]*', '', out)
        return out

    # --- regex extractors ---

    def _regex_imports(self, source: str) -> list[Import]:
        imports: list[Import] = []
        lines = source.split("\n")

        for m in _RE_IMPORT_STAR.finditer(source):
            alias = m.group(1)
            src = m.group(2)
            line = source[:m.start()].count("\n") + 1
            imports.append(Import(name="*", source=src, line=line, alias=alias))

        for m in _RE_IMPORT_NAMED.finditer(source):
            names_str = m.group(1)
            src = m.group(2)
            line = source[:m.start()].count("\n") + 1
            for part in names_str.split(","):
                part = part.strip()
                if not part:
                    continue
                parts = re.split(r'\s+as\s+', part)
                name = parts[0].strip()
                alias = parts[1].strip() if len(parts) > 1 else None
                imports.append(Import(name=name, source=src, line=line, alias=alias))

        for m in _RE_IMPORT_ALIAS.finditer(source):
            src = m.group(1)
            alias = m.group(2)
            line = source[:m.start()].count("\n") + 1
            imports.append(Import(name="*", source=src, line=line, alias=alias))

        for m in _RE_IMPORT_PLAIN.finditer(source):
            src = m.group(1)
            line = source[:m.start()].count("\n") + 1
            # Skip if already captured by another pattern
            already = any(
                i.source == src and i.line == line for i in imports
            )
            if not already:
                imports.append(Import(name="*", source=src, line=line))

        return imports

    def _regex_functions(self, cleaned: str, original: str) -> list[Function]:
        functions: list[Function] = []

        for m in _RE_FUNCTION.finditer(cleaned):
            name = m.group(2)
            params = m.group(3).strip()
            param_count = self._count_params_str(params)
            line = cleaned[:m.start()].count("\n") + 1
            end_line = self._find_block_end(original, line)
            functions.append(Function(
                name=name,
                line=line,
                end_line=end_line,
                param_count=param_count,
                line_count=end_line - line + 1,
            ))

        for m in _RE_SPECIAL_FUNC.finditer(cleaned):
            name = m.group(1)
            params = m.group(2).strip()
            param_count = self._count_params_str(params)
            line = cleaned[:m.start()].count("\n") + 1
            end_line = self._find_block_end(original, line)
            functions.append(Function(
                name=name,
                line=line,
                end_line=end_line,
                param_count=param_count,
                line_count=end_line - line + 1,
            ))

        return functions

    def _regex_classes(self, cleaned: str, original: str) -> list[Class]:
        classes: list[Class] = []

        for m in _RE_CONTRACT.finditer(cleaned):
            name = m.group(1)
            line = cleaned[:m.start()].count("\n") + 1
            end_line = self._find_block_end(original, line)
            # Count methods inside this contract region
            block_text = "\n".join(
                original.split("\n")[line - 1 : end_line]
            )
            method_count = len(_RE_FUNCTION.findall(block_text))
            method_count += len(_RE_SPECIAL_FUNC.findall(block_text))
            classes.append(Class(
                name=name,
                line=line,
                end_line=end_line,
                method_count=method_count,
                line_count=end_line - line + 1,
            ))

        return classes

    def _regex_variables(self, source: str) -> list[Variable]:
        variables: list[Variable] = []
        for m in _RE_STATE_VAR.finditer(source):
            name = m.group(1)
            # Skip if the name looks like a Solidity keyword or type
            if name in (
                "returns", "return", "public", "private", "internal",
                "external", "view", "pure", "payable", "override",
                "virtual", "memory", "storage", "calldata",
                "true", "false",
            ):
                continue
            line = source[:m.start()].count("\n") + 1
            variables.append(Variable(name=name, line=line, scope="module"))
        return variables

    def _regex_calls(self, source: str) -> list[Call]:
        calls: list[Call] = []
        # Keywords that look like function calls but aren't
        skip = {
            "if", "for", "while", "require", "assert", "revert",
            "function", "contract", "interface", "library",
            "mapping", "returns", "return", "new", "delete",
            "type", "catch", "try",
        }
        for m in _RE_CALL.finditer(source):
            raw = m.group(1)
            base_name = raw.split(".")[-1] if "." in raw else raw
            if base_name in skip:
                continue
            line = source[:m.start()].count("\n") + 1
            # Detect emit prefix
            text_before = source[max(0, m.start() - 10):m.start()].strip()
            if text_before.endswith("emit"):
                name = f"emit {raw}"
            else:
                name = raw
            calls.append(Call(name=name, line=line))
        return calls

    # --- shared helpers ---

    def _build_exports(
        self, functions: list[Function], classes: list[Class],
    ) -> list[Export]:
        """In Solidity, all contract names and public/external functions are exports."""
        exports: list[Export] = []
        for cls in classes:
            exports.append(Export(name=cls.name, line=cls.line, kind="class"))
        for fn in functions:
            # Treat all named functions as exports (visibility would need
            # deeper parsing to differentiate; this is good enough for detectors)
            if fn.name not in ("constructor", "fallback", "receive"):
                exports.append(Export(name=fn.name, line=fn.line, kind="function"))
        return exports

    def _count_params_str(self, params: str) -> int:
        """Count parameters from a raw parameter string."""
        params = params.strip()
        if not params:
            return 0
        return len([p for p in params.split(",") if p.strip()])

    def _find_block_end(self, source: str, start_line: int) -> int:
        """Find the closing brace of a block starting at *start_line*.

        Uses brace counting. Returns the line number of the closing brace,
        or the last line of the file if no match is found.
        """
        lines = source.split("\n")
        depth = 0
        found_open = False
        for i in range(start_line - 1, len(lines)):
            for ch in lines[i]:
                if ch == "{":
                    depth += 1
                    found_open = True
                elif ch == "}":
                    depth -= 1
                    if found_open and depth == 0:
                        return i + 1  # 1-indexed
        return len(lines)
