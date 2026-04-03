from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Optional

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.syntax import Syntax

from .config import Config
from .finding import Finding
from .scanner import Scanner
from .state import State

console = Console()

BANNER = """[bold yellow]
 ██╗   ██╗████████╗██████╗ ██╗ ██████╗  ██████╗ ███████╗██████╗
 ██║   ██║╚══██╔══╝██╔══██╗██║██╔════╝ ██╔════╝ ██╔════╝██╔══██╗
 ██║   ██║   ██║   ██████╔╝██║██║  ███╗██║  ███╗█████╗  ██████╔╝
 ╚██╗ ██╔╝   ██║   ██╔══██╗██║██║   ██║██║   ██║██╔══╝  ██╔══██╗
  ╚████╔╝    ██║   ██║  ██║██║╚██████╔╝╚██████╔╝███████╗██║  ██║
   ╚═══╝     ╚═╝   ╚═╝  ╚═╝╚═╝ ╚═════╝  ╚═════╝ ╚══════╝╚═╝  ╚═╝[/bold yellow]
[dim]codebase health scanner by loser labs[/dim]
"""


def _show_banner():
    """Print the vtrigger ASCII banner."""
    from . import __version__
    console.print(BANNER)
    console.print(f"  [bold]v{__version__}[/bold]   [dim]|[/dim]   20 languages   [dim]|[/dim]   6 detectors   [dim]|[/dim]   `vtrigger scan .` to start")
    console.print()


def _show_first_run():
    """Show a welcome message on first run in a project."""
    console.print()
    console.print(Panel(
        "[bold]First time here.[/bold]\n\n"
        "Run [green]`vtrigger scan .`[/green] to find dead code, unused imports, duplicated logic, hardcoded secrets, and more.\n"
        "Run [green]`vtrigger init`[/green] to create a config file.\n"
        "Run [green]`vtrigger --help`[/green] for all commands.",
        title="[bold yellow]vtrigger[/bold yellow]",
        border_style="yellow",
    ))
    console.print()


def _ensure_gitignore(project_path: Path) -> None:
    """Add .vtrigger/ to .gitignore if not already present."""
    gitignore = project_path / ".gitignore"
    marker = ".vtrigger/"

    if gitignore.exists():
        content = gitignore.read_text()
        if marker in content:
            return
        if not content.endswith("\n"):
            content += "\n"
        content += f"{marker}\n"
        gitignore.write_text(content)
    else:
        gitignore.write_text(f"{marker}\n")


def _save_current(state: State, finding_hash: str) -> None:
    """Store the current finding hash in .vtrigger/current."""
    (state.dir / "current").write_text(finding_hash)


def _load_current(state: State) -> Optional[str]:
    """Load the current finding hash from .vtrigger/current."""
    current_path = state.dir / "current"
    if current_path.exists():
        return current_path.read_text().strip()
    return None


def _show_finding(finding, remaining: int) -> None:
    """Render a single finding as a Rich panel."""
    from rich.text import Text
    from rich.console import Group

    parts = []
    parts.append(Text.from_markup(f"[bold]Detector:[/bold]   {finding.detector}"))
    parts.append(Text.from_markup(f"[bold]Category:[/bold]   {finding.category}"))
    parts.append(Text.from_markup(f"[bold]Location:[/bold]   {finding.file}:{finding.line}"))
    parts.append(Text.from_markup(f"[bold]Confidence:[/bold] {finding.confidence}"))
    parts.append(Text(""))
    parts.append(Text.from_markup(f"[yellow]{finding.message}[/yellow]"))

    if finding.snippet:
        # Detect language from file extension
        ext_map = {".py": "python", ".js": "javascript", ".ts": "typescript",
                   ".tsx": "tsx", ".jsx": "jsx", ".sol": "solidity"}
        ext = Path(finding.file).suffix
        lang = ext_map.get(ext, "text")
        parts.append(Text(""))
        parts.append(Syntax(finding.snippet, lang, theme="monokai", line_numbers=False))

    console.print()
    console.print(Panel(
        Group(*parts),
        title=f"[bold]{finding.hash}[/bold]",
        subtitle=f"[dim]{remaining} remaining[/dim]",
        border_style="yellow",
    ))
    console.print()


@click.group(invoke_without_command=True)
@click.option("--version", is_flag=True, help="Show version and exit.")
@click.pass_context
def main(ctx, version):
    """vtrigger, codebase health scanner."""
    if version:
        _show_banner()
        return
    if ctx.invoked_subcommand is None:
        _show_banner()
        console.print(ctx.get_help())
        console.print()


_INIT_CONFIG_TEMPLATE = """\
# vtrigger configuration
# Uncomment and modify values to override defaults.

# Patterns to ignore (added to built-in defaults like node_modules, dist, etc.)
# ignore:
#   - "generated/**"
#   - "vendor/**"

# Thresholds for the size detector
# thresholds:
#   max_file_lines: 500
#   max_function_lines: 100
#   max_class_methods: 20
#   duplication_min_copies: 3

# Disable specific detectors
# detectors:
#   disabled:
#     - secrets

# Allowlist: skip specific findings by detector
# allowlist:
#   dead_code:
#     - "src/pages/**"
#     - "src/app/**/page.tsx"
"""


@main.command()
@click.argument("path", default=".", type=click.Path(exists=True))
def init(path: str):
    """Initialize a vtrigger config in the target directory."""
    project_path = Path(path).resolve()
    vtrigger_dir = project_path / ".vtrigger"
    config_path = vtrigger_dir / "config.yaml"

    if config_path.exists():
        console.print(f" [yellow]Config already exists:[/yellow] {config_path}")
        return

    vtrigger_dir.mkdir(parents=True, exist_ok=True)
    config_path.write_text(_INIT_CONFIG_TEMPLATE)
    console.print(f" [green]Created[/green] {config_path}")

    _ensure_gitignore(project_path)
    console.print(f" [green]Ensured[/green] .vtrigger/ is in .gitignore")


@main.command()
@click.argument("path", default=".", type=click.Path(exists=True))
@click.option("--fresh", is_flag=True, help="Force a full re-parse, ignoring cache.")
@click.option("--json", "output_json", is_flag=True, help="Output findings as JSON.")
def scan(path: str, fresh: bool, output_json: bool):
    """Scan a codebase for issues."""
    project_path = Path(path).resolve()

    # First run welcome
    vtrigger_dir = Path.cwd() / ".vtrigger"
    if not vtrigger_dir.exists() and not output_json:
        _show_first_run()

    config = Config.load(project_path)
    scanner = Scanner(project_path, config)

    # Scan
    if output_json:
        files = scanner.discover_files()
    else:
        with console.status("[bold]Discovering files..."):
            files = scanner.discover_files()
        console.print(f" Scanning {len(files)} files...")
        console.print()

    # Save state to cwd (where .vtrigger/ lives), not the scanned path
    cwd = Path.cwd()
    state = State(cwd)

    if output_json:
        if fresh:
            parsed = scanner.parse_files(files)
        else:
            parsed = scanner.parse_files_incremental(files, state)
        findings = scanner.detect(parsed)
    else:
        with console.status("[bold]Analyzing..."):
            if fresh:
                parsed = scanner.parse_files(files)
            else:
                parsed = scanner.parse_files_incremental(files, state)
            findings = scanner.detect(parsed)

    # Group findings by detector
    by_detector: dict[str, list] = {}
    for f in findings:
        by_detector.setdefault(f.detector, []).append(f)

    state.save_findings(findings)

    if output_json:
        output = {
            "total": len(findings),
            "by_detector": {k: len(v) for k, v in by_detector.items()},
            "findings": [f.to_dict() for f in findings],
        }
        click.echo(json.dumps(output, indent=2))
    else:
        # Display grouped results
        for detector_name, detector_findings in by_detector.items():
            count = len(detector_findings)
            label = detector_name.replace("_", " ")
            console.print(Panel(
                f"  [bold]{count}[/bold] {label}",
                title=f"[bold]{detector_name}[/bold]",
                border_style="yellow",
            ))
        console.print()

        total = len(findings)
        if total > 0:
            console.print(f" [bold]{total}[/bold] findings. Run [green]`vtrigger next`[/green] to start.")
        else:
            console.print(" [green]No issues found. Clean codebase.[/green]")

    # Ensure .vtrigger/ is in .gitignore (in cwd)
    _ensure_gitignore(cwd)


@main.command(name="next")
@click.option("--detector", default=None, help="Filter by detector name.")
@click.option("--path", default=".", type=click.Path(exists=True), help="Project path.")
def next_finding(detector: Optional[str], path: str):
    """Show the next pending finding."""
    project_path = Path(path).resolve()
    state = State(project_path)

    current_hash = _load_current(state)
    pending = state.pending
    if detector:
        pending = [f for f in pending if f.detector == detector]

    # Find the next finding after the current one
    finding = None
    if current_hash:
        # Skip past the current finding
        found_current = False
        for f in pending:
            if f.hash == current_hash:
                found_current = True
                continue
            if found_current:
                finding = f
                break
        # If current wasn't found in pending (resolved/skipped), just take first
        if not found_current:
            finding = pending[0] if pending else None
    else:
        finding = pending[0] if pending else None

    if finding is None:
        console.print()
        console.print(" [green]All clear.[/green]")
        console.print()
        return

    remaining = len([f for f in pending if f.hash != current_hash])

    _save_current(state, finding.hash)
    _show_finding(finding, remaining)


@main.command()
@click.option("--path", default=".", type=click.Path(exists=True), help="Project path.")
def resolve(path: str):
    """Resolve the current finding."""
    project_path = Path(path).resolve()
    state = State(project_path)

    current_hash = _load_current(state)
    if not current_hash:
        console.print()
        console.print(" [red]No current finding. Run `vtrigger next` first.[/red]")
        console.print()
        return

    state.resolve(current_hash)
    console.print(f" [green]Resolved[/green] {current_hash}")

    # Auto-show next
    finding = state.next_finding()
    if finding:
        remaining = len(state.pending)
        _save_current(state, finding.hash)
        _show_finding(finding, remaining)
    else:
        console.print()
        console.print(" [green]All clear.[/green]")
        console.print()


@main.command()
@click.option("--path", default=".", type=click.Path(exists=True), help="Project path.")
def skip(path: str):
    """Skip the current finding."""
    project_path = Path(path).resolve()
    state = State(project_path)

    current_hash = _load_current(state)
    if not current_hash:
        console.print()
        console.print(" [red]No current finding. Run `vtrigger next` first.[/red]")
        console.print()
        return

    state.skip(current_hash)
    console.print(f" [dim]Skipped[/dim] {current_hash}")

    # Auto-show next
    finding = state.next_finding()
    if finding:
        remaining = len(state.pending)
        _save_current(state, finding.hash)
        _show_finding(finding, remaining)
    else:
        console.print()
        console.print(" [green]All clear.[/green]")
        console.print()


@main.command(name="list")
@click.option("--pending", is_flag=True, help="Show only pending findings.")
@click.option("--detector", default=None, help="Filter by detector name.")
@click.option("--path", default=".", type=click.Path(exists=True), help="Project path.")
def list_findings(pending: bool, detector: Optional[str], path: str):
    """List all findings."""
    project_path = Path(path).resolve()
    state = State(project_path)

    findings = state._findings
    resolved = state._resolved
    skipped = state._skipped

    if pending:
        findings = [f for f in findings if f.hash not in resolved and f.hash not in skipped]
    if detector:
        findings = [f for f in findings if f.detector == detector]

    if not findings:
        console.print()
        console.print(" [dim]No findings match your filters.[/dim]")
        console.print()
        return

    table = Table(title="Findings")
    table.add_column("Hash", style="dim", width=12)
    table.add_column("Status", width=10)
    table.add_column("Detector", style="cyan")
    table.add_column("File", style="white")
    table.add_column("Line", justify="right")
    table.add_column("Message")

    for f in findings:
        if f.hash in resolved:
            status = "[green]resolved[/green]"
        elif f.hash in skipped:
            status = "[dim]skipped[/dim]"
        else:
            status = "[yellow]pending[/yellow]"

        table.add_row(
            f.hash,
            status,
            f.detector,
            f.file,
            str(f.line) if f.line else "",
            f.message,
        )

    console.print()
    console.print(table)
    console.print()


@main.command()
@click.option("--path", default=".", type=click.Path(exists=True), help="Project path.")
def stats(path: str):
    """Show finding counts by detector."""
    project_path = Path(path).resolve()
    state = State(project_path)

    findings = state._findings
    resolved = state._resolved
    skipped = state._skipped

    # Group by detector
    detectors: dict[str, dict[str, int]] = {}
    for f in findings:
        if f.detector not in detectors:
            detectors[f.detector] = {"pending": 0, "resolved": 0, "skipped": 0}
        if f.hash in resolved:
            detectors[f.detector]["resolved"] += 1
        elif f.hash in skipped:
            detectors[f.detector]["skipped"] += 1
        else:
            detectors[f.detector]["pending"] += 1

    if not detectors:
        console.print()
        console.print(" [dim]No findings. Run `vtrigger scan` first.[/dim]")
        console.print()
        return

    table = Table(title="Stats")
    table.add_column("Detector", style="cyan")
    table.add_column("Pending", justify="right", style="yellow")
    table.add_column("Resolved", justify="right", style="green")
    table.add_column("Skipped", justify="right", style="dim")
    table.add_column("Total", justify="right", style="bold")

    for name, counts in sorted(detectors.items()):
        total = counts["pending"] + counts["resolved"] + counts["skipped"]
        table.add_row(
            name,
            str(counts["pending"]),
            str(counts["resolved"]),
            str(counts["skipped"]),
            str(total),
        )

    console.print()
    console.print(table)
    console.print()


@main.command()
@click.argument("path", default=".", type=click.Path(exists=True))
@click.option("--base", default="main", help="Branch to diff against (default: main).")
@click.option("--all", "review_all", is_flag=True, help="Review all files, not just changed ones.")
@click.option("--fresh", is_flag=True, help="Force a full re-parse, ignoring cache.")
@click.option("--json", "output_json", is_flag=True, help="Output findings as JSON.")
def review(path: str, base: str, review_all: bool, fresh: bool, output_json: bool):
    """Review changed files for code quality issues."""
    from .scanner import SUPPORTED_EXTENSIONS

    project_path = Path(path).resolve()
    config = Config.load(project_path)
    scanner = Scanner(project_path, config)

    if review_all:
        # Same as scan but branded as review
        if output_json:
            files = scanner.discover_files()
        else:
            with console.status("[bold]Discovering files..."):
                files = scanner.discover_files()

        changed_label = f"all {len(files)} files"
        changed_set = None  # No filtering needed
    else:
        # Find changed files via git
        changed_files = _git_changed_files(project_path, base)

        if not changed_files:
            if output_json:
                click.echo(json.dumps({"total": 0, "by_detector": {}, "findings": []}, indent=2))
            else:
                console.print()
                console.print(" [green]No changed files to review.[/green]")
                console.print()
            return

        # Filter to supported extensions
        changed_files = [
            f for f in changed_files
            if Path(f).suffix in SUPPORTED_EXTENSIONS
        ]

        if not changed_files:
            if output_json:
                click.echo(json.dumps({"total": 0, "by_detector": {}, "findings": []}, indent=2))
            else:
                console.print()
                console.print(" [green]No supported files changed.[/green]")
                console.print()
            return

        # Convert to absolute paths
        changed_abs = {str((project_path / f).resolve()) for f in changed_files}
        changed_label = f"{len(changed_abs)} changed files (vs {base})"
        changed_set = changed_abs

        # We still need all files for cross-file analysis
        if output_json:
            files = scanner.discover_files()
        else:
            with console.status("[bold]Discovering files..."):
                files = scanner.discover_files()

    if not output_json:
        console.print(f" Reviewing {changed_label}...")
        console.print()

    # Save state
    cwd = Path.cwd()
    state = State(cwd)

    if output_json:
        if fresh:
            parsed = scanner.parse_files(files)
        else:
            parsed = scanner.parse_files_incremental(files, state)
        findings = scanner.detect(parsed)
    else:
        with console.status("[bold]Analyzing..."):
            if fresh:
                parsed = scanner.parse_files(files)
            else:
                parsed = scanner.parse_files_incremental(files, state)
            findings = scanner.detect(parsed)

    # Filter findings to only changed files (unless --all)
    if changed_set is not None:
        findings = [
            f for f in findings
            if str((project_path / f.file).resolve()) in changed_set
        ]

    # Group findings by detector
    by_detector: dict[str, list] = {}
    for f in findings:
        by_detector.setdefault(f.detector, []).append(f)

    state.save_findings(findings)

    if output_json:
        output = {
            "total": len(findings),
            "by_detector": {k: len(v) for k, v in by_detector.items()},
            "findings": [f.to_dict() for f in findings],
        }
        click.echo(json.dumps(output, indent=2))
    else:
        # Display grouped results
        for detector_name, detector_findings in by_detector.items():
            count = len(detector_findings)
            label = detector_name.replace("_", " ")
            console.print(Panel(
                f"  [bold]{count}[/bold] {label}",
                title=f"[bold]{detector_name}[/bold]",
                border_style="yellow",
            ))
        console.print()

        total = len(findings)
        if total > 0:
            console.print(f" [bold]{total}[/bold] findings in changed files. Run [green]`vtrigger next`[/green] to start.")
        else:
            console.print(" [green]No issues found in changed files.[/green]")

    _ensure_gitignore(cwd)


@main.command()
@click.argument("path", default=".", type=click.Path(exists=True))
@click.option("--dry-run", is_flag=True, help="Show what would be fixed without making changes.")
@click.option("--detector", default=None, help="Only fix findings from a specific detector.")
def fix(path: str, dry_run: bool, detector: Optional[str]):
    """Auto-fix safe-to-remove issues (currently: unused imports)."""
    project_path = Path(path).resolve()
    config = Config.load(project_path)
    scanner = Scanner(project_path, config)

    # 1. Run a scan to get findings
    with console.status("[bold]Scanning..."):
        files = scanner.discover_files()
        parsed = scanner.parse_files(files)
        findings = scanner.detect(parsed)

    # 2. Filter to only auto-fixable findings (unused_imports)
    fixable_detectors = {"unused_imports"}
    fixable = [
        f for f in findings
        if f.detector in fixable_detectors
    ]
    if detector:
        fixable = [f for f in fixable if f.detector == detector]

    if not fixable:
        console.print()
        console.print(" [green]Nothing to fix.[/green]")
        console.print()
        return

    # 3. Group fixes by file
    by_file: dict[str, list[Finding]] = {}
    for f in fixable:
        by_file.setdefault(f.file, []).append(f)

    fixed_count = 0
    fixed_files = 0
    fix_log: list[str] = []

    # 4. For each file, sort fixes by line number DESCENDING
    for rel_path, file_findings in sorted(by_file.items()):
        abs_path = project_path / rel_path
        try:
            lines = abs_path.read_text(encoding="utf-8").splitlines(keepends=True)
        except (OSError, UnicodeDecodeError):
            continue

        file_findings.sort(key=lambda f: f.line or 0, reverse=True)
        file_changed = False

        for finding in file_findings:
            if finding.line is None or finding.snippet is None:
                continue

            line_idx = finding.line - 1
            if line_idx < 0 or line_idx >= len(lines):
                continue

            current_line = lines[line_idx].rstrip("\n").rstrip("\r")
            expected = finding.snippet.strip()

            # Verify the line still matches what the finding expects
            if current_line.strip() != expected:
                continue

            # Check if this is a single-name import line.
            # For multi-name imports like "from foo import bar, baz", we skip
            # because we can't safely remove just one name without parsing.
            if _is_multi_name_import(current_line):
                continue

            # Safe to remove this line
            if not dry_run:
                lines.pop(line_idx)

            action = "removed" if not dry_run else "would remove"
            fix_log.append(f"  {rel_path}:{finding.line} {action} `{expected}`")
            fixed_count += 1
            file_changed = True

        if file_changed:
            fixed_files += 1
            if not dry_run:
                abs_path.write_text("".join(lines), encoding="utf-8")

    # 7. Show summary
    console.print()
    if dry_run:
        console.print(f" Would fix {fixed_count} unused imports across {fixed_files} files.")
    else:
        console.print(f" Fixed {fixed_count} unused imports across {fixed_files} files.")
    console.print()

    for entry in fix_log:
        console.print(entry)

    if fix_log:
        console.print()

    if dry_run:
        console.print(" Run [green]`vtrigger fix`[/green] without --dry-run to apply.")
        console.print()


def _is_multi_name_import(line: str) -> bool:
    """Check if an import line imports multiple names.

    Returns True for lines like:
      from foo import bar, baz
      import os, sys

    Returns False for single-name imports like:
      import os
      from foo import bar
      from foo import bar as b
    """
    stripped = line.strip()

    if stripped.startswith("from ") and " import " in stripped:
        # Extract the part after "import"
        import_part = stripped.split(" import ", 1)[1]
        # Remove trailing comments
        if "#" in import_part:
            import_part = import_part[:import_part.index("#")]
        # Check for comma (multiple names)
        return "," in import_part

    if stripped.startswith("import "):
        import_part = stripped[len("import "):]
        # Remove trailing comments
        if "#" in import_part:
            import_part = import_part[:import_part.index("#")]
        if "//" in import_part:
            import_part = import_part[:import_part.index("//")]
        # JS/TS: import { foo, bar } from 'module' -- check brace content
        if "{" in import_part and "}" in import_part:
            brace_content = import_part.split("{", 1)[1].split("}", 1)[0]
            return "," in brace_content
        # Python: import os, sys or JS: import foo, bar
        return "," in import_part

    return False


def _git_changed_files(project_path: Path, base: str) -> list[str]:
    """Get list of changed files relative to the project path using git."""
    # Try three-dot diff first (branch comparison)
    result = subprocess.run(
        ["git", "diff", "--name-only", f"{base}...HEAD"],
        cwd=project_path,
        capture_output=True,
        text=True,
    )

    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip().splitlines()

    # Fallback: diff against HEAD (uncommitted changes)
    result = subprocess.run(
        ["git", "diff", "--name-only", "HEAD"],
        cwd=project_path,
        capture_output=True,
        text=True,
    )

    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip().splitlines()

    # Fallback: git status for untracked/staged files
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=project_path,
        capture_output=True,
        text=True,
    )

    if result.returncode == 0 and result.stdout.strip():
        files = []
        for line in result.stdout.strip().splitlines():
            # Porcelain format: "XY filename" where XY is 2-char status
            if len(line) > 3:
                files.append(line[3:].strip())
        return files

    return []
