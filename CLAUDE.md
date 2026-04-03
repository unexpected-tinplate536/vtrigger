# vtrigger

Codebase health scanner. Python CLI, installed via `pip install vtrigger`.

## For using vtrigger as a tool

If you're using vtrigger to clean up a codebase, here's the workflow:

```bash
vtrigger scan .              # find problems
vtrigger next                # look at the first one
vtrigger resolve             # after you fix it
vtrigger skip                # if it's not worth fixing
vtrigger fix . --dry-run     # preview auto-fixable issues
vtrigger fix .               # auto-remove unused imports
```

Only fix real problems. Skip style nits, skip "could be abstracted" suggestions, skip size warnings on clear code. Security findings (secrets detector) should always be fixed.

Use `vtrigger review .` instead of `vtrigger scan .` to only check files changed since main.

Use `vtrigger scan . --json` for structured output you can parse programmatically.

## Architecture (for AI agents reading this repo)

The internal Python package is `omega`. Parsers live in `src/omega/parsers/`, detectors in `src/omega/detectors/`. The CLI entry point is `src/omega/cli.py`. State persists in `.vtrigger/` inside the scanned project.
