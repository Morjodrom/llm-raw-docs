# Local docs: unzip and run

Target: **macOS 14+ / Apple Silicon ARM64 / ordinary CPython 3.13** with
`venv`, `ensurepip` and SQLite FTS5. Intel/Rosetta, free-threaded Python,
other Python versions, Linux and Windows are not supported by this ZIP.
Python itself is NOT included. A compatible Python must already be installed.

```bash
unzip local-docs-macos-arm64-py313.zip
cd local-docs
# If needed: export LOCAL_DOCS_PYTHON=/absolute/path/to/python3.13
EXPORT="/absolute/path/to/raw-export"
bash local-docs.sh update --input "$EXPORT"
bash local-docs.sh status --input "$EXPORT"
bash local-docs.sh search --input "$EXPORT" 'ошибки подключения' --limit 5
bash local-docs.sh read --input "$EXPORT" SECTION_ID
```

No setup command or network access is needed. First use creates `.local-docs-env/`
from the included hashed wheels, including Russian dictionaries. It can take a
minute and requires writable extraction storage and space for the environment.
Install progress is on stderr; workflow JSON stays on stdout. Subsequent calls
reuse the environment. Source files are loaded directly, not copied into it.
Automatic interpreter selection is `python3.13`, then `python3`; an incompatible
selected interpreter produces an error, never an automatic Python download.
Use absolute paths without symlink components (macOS `/private/tmp`, not `/tmp`).
Do not put source documentation in a parent of the extracted tool directory.

`update` is normalization → incremental indexing; search/read use that snapshot.
Exit **0** success, **1** usable partial update (inspect reports), **2** fatal/setup,
**3** missing section. A fatal refresh invalidates readiness; don't query old
indexes through other tools. `status` is not a live source-change detector.
Read complete returned sections and provenance before quoting. See
[workflow details](LLM-docs/README.md) for recovery, citations and limitations.
All document operations are offline. Installation is also offline in the ZIP;
the Python socket guard is defense-in-depth, not an OS sandbox for arbitrary code.
No source documents are bundled or modified.

## Edit and redistribute

Edit `docnorm/src/`, `docsearch/`, the workflow, or the canonical skill at
`LLM-docs/skills/local-docs-search/`. Rebuild from this tree or the source repository:

```bash
bash LLM-docs/build-distribution.sh
# Rebuild using only cached wheels:
bash LLM-docs/build-distribution.sh --offline
```

Output: `LLM-docs/dist/local-docs-macos-arm64-py313.zip`. First repository build
requires pip and internet; cached/ZIP builds do not. Dependencies remain pinned;
changing them requires rebuilding a complete compatible wheelhouse. The build
includes current source files, including uncommitted edits, and refreshes the
skill discovery copy. It excludes exports, normalized collections, search DBs,
virtual environments, caches and credentials. `manifest.json` records the base
workflow commit and SHA-256 of each payload file. ZIP timestamps, ordering and
permissions are fixed; identical inputs and wheels produce identical archives
with the same Python/zlib builder. Dependency wheels retain their license metadata.

After moving the extracted directory, the next launch recreates only its owned
environment (virtual environments themselves aren't relocatable). The next update uses a new state namespace bound to the new bundle/export
paths; old collections are not silently rebound or deleted. Never copy a virtual environment into the distribution.

## Agent skill

The ZIP includes a real `.agents/skills/local-docs-search/` directory, not a
symlink, and root `AGENTS.md` explicitly tells project agents to load it.
Start an agent in the extracted directory; restart its session if its inventory
was loaded earlier. Native discovery in every agent client is not guaranteed;
explicitly loading that SKILL.md is the fallback. No global configuration changes.

## Recovery

Bad/incomplete wheels: restore from the original ZIP; never fall back to online
installation. Unsupported Python/FTS5: select a compatible installed interpreter.
After interrupted provisioning, ensure no launcher is running, then remove only
the empty `.local-docs-install.lock/` with `rmdir` and retry. Interrupted owned
`.local-docs-env/` is recreated automatically. An unowned directory is never deleted.
Do not modify dependency hash manifests to conceal corruption. Hashes detect
corruption, not authenticity: obtain the archive from a trusted sender.
