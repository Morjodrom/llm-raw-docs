# Repository Guidelines

These guidelines apply only to `docnorm/`.

## Project Structure & Module Organization

- `src/docnorm/` contains the Python CLI, scanner, adapters, normalization, reconciliation, link rewriting, safety checks and atomic publishing.
- `tests/` contains pytest fixtures and regression tests. `scripts/validate_collection.py` validates the actual export.
- `pyproject.toml` defines packaging and the CLI entry point; `requirements.lock` pins the tested environment.
- `input-hashes.json` and `validation-report.json` record export-validation evidence. `README.md` documents supported formats and limitations.
- The sample input and generated collection live outside this project, in sibling directories. Treat inputs as immutable and regenerate outputs through the CLI.

## Build, Test, and Development Commands

Use Python 3.11+; the existing environment uses Python 3.13. Run from this project directory:

```sh
python3.13 -m venv .venv
.venv/bin/pip install -r requirements.lock
.venv/bin/pip install --no-deps --no-build-isolation -e .
.venv/bin/pytest -q
.venv/bin/docnorm normalize ../drive-download-20261012T111012Z-1-001 --output ../normalized
.venv/bin/python scripts/validate_collection.py
```

The first three commands install the pinned development environment and editable CLI. Pytest runs unit/regression tests. Normalization regenerates the collection; validation checks source hashes, provenance, links and deterministic reruns. Exit code `1` means partial/failed conversions with a report; `2` indicates fatal setup or publication errors.

## Coding Style & Naming Conventions

Use four-space indentation, `snake_case` functions/modules and `PascalCase` classes. Follow existing dataclasses and component boundaries; keep format-specific logic in adapters rather than the CLI. Use UTF-8 and LF endings. Preserve meaningful content and report uncertainty instead of inventing summaries or source-line mappings. No formatter or linter is currently configured.

## Testing Guidelines

Name tests `test_<behavior>` and use pytest's `tmp_path` for isolated inputs/outputs. Add regression cases for each changed format, link rule or failure mode. Maintain coverage of Unicode, duplicates, malformed files, include cycles, symlinks, offline operation and stale-output cleanup. Networking is blocked by the test fixture. Do not replace `input-hashes.json` merely to conceal source changes.

## Commit & Pull Request Guidelines

No Git metadata is present, so existing commit conventions cannot be verified. Use concise imperative messages, such as `Fix include-cycle reporting`. Describe behavioral changes, link relevant issues, include test results and explain changed conversion warnings or output contracts. Exclude virtual environments and caches; summarize regenerated export artifacts separately.

## Safety & Agent Instructions

Never modify inputs, execute their contents, fetch remote links or follow symlinks. Delete only stale tool-owned outputs. Do not run concurrent writers against one output directory. Report unavailable skills or incomplete validation explicitly; scope any necessary unsandboxed CLI operation narrowly to the task.
