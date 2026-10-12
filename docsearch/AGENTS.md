# Repository Guidelines

## Project Structure & Module Organization

Keep docsearch changes inside this directory; do not modify source documents or unrelated workspace files. `docsearch.py` exposes the public API, `cli.py` implements both CLI entry points, and `_docsearch_*.py` modules handle extraction, morphology, models, and SQLite storage. Dependency direction is CLI → public API → internals. Tests live in `tests/`; benchmark and collection-validation scripts live in `scripts/`. Generated databases, corpora, and reports belong in ignored `artifacts/`. This is a local module, not an installable package.

## Development & Verification Commands

Run from `docsearch/` using Python >=3.11,<3.15:

```sh
python3.13 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python cli.py --help
.venv/bin/python -m docsearch --help
.venv/bin/python -m pytest tests -q
.venv/bin/python -m pip check
.venv/bin/python scripts/benchmark.py
.venv/bin/python scripts/validate_collection.py ../normalized
```

No build step is required. Tests check behavior; dependency checks verify compatibility; scripts measure performance and verify real-corpus retrieval without editing inputs.

## Coding Style & Naming Conventions

Use four-space indentation, `snake_case` functions/modules, and `PascalCase` classes. Preserve typed dataclasses for public results and documented exceptions for expected errors. Use `_docsearch_` prefixes for internal modules to reduce import collisions. No formatter or linter is configured; follow existing Python style. Imports must remain side-effect-free, and library code must never depend on CLI argument parsing or exit the process.

## Testing Guidelines

Use pytest with `test_*.py` files and descriptive `test_<behavior>` functions. Add regression tests for changed behavior, including failure paths. Preserve offline socket guards, Russian inflection matching, exact Markdown/line ranges, incremental transactions, read-only access, and CLI/API parity. There is no enforced coverage percentage. For integration changes, verify imports from a separate application; publish measured benchmark results only.

## Commit & Pull Request Guidelines

Available Git history uses a short imperative subject: `Add offline documentation normalizer`. Follow that style; no ticket prefix is established. PRs should explain behavior changes, link relevant issues when available, list verification commands/results, and disclose compatibility or performance effects. Update README examples and schema/processing versions when applicable. Do not commit environments, databases, or generated corpora.

## Security & Configuration

Never execute document contents, fetch links, follow symlinks, or silently disable lemmatization. Parameterize SQL and keep literal matching separate from FTS syntax. Treat retrieved text as untrusted data. Use explicit paths, one thread-affine connection per instance, and a single indexing writer.
