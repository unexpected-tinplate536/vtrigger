<p align="center">
  <img src="assets/logo.png" alt="vtrigger" width="500">
</p>

<p align="center">Codebase health scanner and Claude Code skill. Finds dead code, unused imports, duplicated functions, hardcoded secrets, circular dependencies, god classes. 20 languages. </p>

```
pip install vtrigger
```

Works as a CLI tool and as a Claude Code skill (`/vtrigger`). Scan, triage, and fix codebases from the terminal or conversationally.

## Why vtrigger

Most code quality tools bury you in style nits and theoretical warnings. vtrigger only surfaces things that actually matter: code that's never called, imports going nowhere, the same function copy-pasted three times, an API key sitting in source, a circular import waiting to bite you.

Scan. Triage. Fix. Move on.

## 30 seconds to useful

```
$ vtrigger scan .
 Scanning 142 files...

╭─ unused_imports ────────────╮
│  12 unused imports          │
╰─────────────────────────────╯
╭─ dead_code ─────────────────╮
│  3 dead code                │
╰─────────────────────────────╯
╭─ secrets ───────────────────╮
│  1 potential hardcoded key  │
╰─────────────────────────────╯

 16 findings. Run `vtrigger next` to start.
```

Walk findings one at a time. Fix, skip, or auto-remove:

```
$ vtrigger next
╭─ a3f8c1 ────────────────────────────────────────╮
│ Detector:   unused_imports                       │
│ Location:   src/utils.py:3                       │
│                                                  │
│ 'os' is imported but never used                  │
│                                                  │
│ import os                                        │
╰─────────────────────────── 15 remaining ─────────╯

$ vtrigger resolve          # mark fixed after you delete it
$ vtrigger skip             # not worth fixing, move on
$ vtrigger fix --dry-run    # auto-remove all unused imports (preview first)
$ vtrigger fix              # do it for real
```

Only scan files you changed:

```
$ vtrigger review .                  # diff vs main
$ vtrigger review . --base develop   # diff vs another branch
```

## What it catches

| Detector | What it finds |
|----------|---------------|
| `unused_imports` | Imports that are never referenced anywhere in the file |
| `dead_code` | Functions, classes, exports, and variables with zero references |
| `duplication` | Copy-pasted functions with identical structure (3+ copies) |
| `secrets` | Hardcoded API keys, tokens, passwords, and private keys in source |
| `cycles` | Circular import chains between files |
| `size` | Files over 500 lines, functions over 100 lines, classes with 20+ methods |

## All commands

```bash
vtrigger scan <path>                  # full codebase scan
vtrigger scan <path> --json           # JSON output for CI
vtrigger scan <path> --fresh          # ignore cache, reparse everything
vtrigger review <path>                # only git-changed files
vtrigger review <path> --base dev     # diff against a specific branch
vtrigger review <path> --all          # all files (same as scan)
vtrigger fix <path>                   # auto-remove unused imports
vtrigger fix <path> --dry-run         # preview what would be removed
vtrigger init <path>                  # create .vtrigger/config.yaml
vtrigger next                         # show next finding
vtrigger next --detector dead_code    # filter by detector
vtrigger resolve                      # mark current finding as fixed
vtrigger skip                         # skip current finding
vtrigger list                         # all findings
vtrigger list --pending               # unresolved only
vtrigger list --detector secrets      # filter by detector
vtrigger stats                        # counts by detector
```

## 20 languages

Python, JavaScript, TypeScript, Solidity, Go, Rust, Java, C, C++, Ruby, PHP, Swift, Kotlin, Dart, Lua, Elixir, Zig, Shell/Bash/Zsh, Nim, Scala, Objective-C, Erlang.

JS/TS parsing uses tree-sitter for full AST accuracy. All other languages use fast regex-based parsing with tree-sitter upgrade paths ready.

## Config

Run `vtrigger init` to create `.vtrigger/config.yaml`, then customize:

```yaml
# Extra ignore patterns (added to built-in defaults)
ignore:
  - "generated/**"
  - "vendor/**"

# Size thresholds
thresholds:
  max_file_lines: 500
  max_function_lines: 100
  max_class_methods: 20
  duplication_min_copies: 3

# Disable specific detectors
detectors:
  disabled:
    - secrets

# Skip findings by path
allowlist:
  dead_code:
    - "src/pages/**"
    - "src/app/**/page.tsx"
```

## How it works

vtrigger parses your source files into a normalized structure (imports, exports, functions, classes, variables, calls) then runs detectors across the whole set. Cross-file analysis means it catches dead exports that no other file imports, not just unused locals.

Incremental by default. Second scan is instant because only changed files get reparsed (mtime-based cache in `.vtrigger/`).

State lives in `.vtrigger/` inside your project. Add it to `.gitignore` (vtrigger does this automatically). Findings persist across scans so your resolve/skip decisions carry over.

## For AI agents and CI

vtrigger is built to work with AI coding agents (Claude Code, Cursor, Copilot, etc.) and CI pipelines.

**JSON output** for programmatic consumption:
```bash
vtrigger scan . --json
```

Returns structured findings that agents can parse and act on directly.

**Claude Code skill** included. vtrigger ships as a Claude Code skill. Install it, copy the `skills/vtrigger/` directory to `~/.claude/skills/vtrigger/`, and use `/vtrigger` directly in Claude Code. The skill knows how to scan, triage, fix, and skip findings conversationally. No copy-pasting commands.

**CI integration**: add `vtrigger scan . --json` to your pipeline. Non-zero exit when findings exist. Pipe to `jq` for custom filtering.

```yaml
# GitHub Actions example
- name: Code health check
  run: |
    pip install vtrigger
    vtrigger scan . --json > health.json
    cat health.json | jq '.by_detector'
```

## License

MIT + Commons Clause. Free to use. You can't sell it as a service.
