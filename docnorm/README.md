# docnorm

A local, offline Python CLI that converts mixed documentation into traceable Markdown. It never invokes an LLM, fetches URLs, executes utilities, evaluates spreadsheet formulas, renders JavaScript, or performs OCR. The implementation, tests and environment live in this dedicated folder; sources and normalized output remain separate.

## Installation

Requires Python **3.11+**. This export was tested with Python 3.13.10 on macOS ARM64.

```sh
cd /Users/mediator/survival-docs/docnorm
python3.13 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip install --no-deps --no-build-isolation -e .
```

`requirements.lock` pins the tested dependencies, including the build backend and pytest. Installation can download packages; normalization cannot. For offline installation, provision compatible wheels first and add `--no-index --find-links /path/to/wheels` to the dependency-install command. The lock lists exact versions, not wheel hashes, and different platforms must have compatible wheels.

## Usage

```sh
.venv/bin/docnorm normalize ../drive-download-20261012T111012Z-1-001 \
  --output ../normalized
# Or, after activating the environment:
docnorm normalize ./source --output ./normalized
```

Exit codes: **0** all documents successful; **1** partial documents or failed files (outputs/report still written); **2** invalid arguments, missing dependencies or fatal publication/setup errors. Skips and confidently merged representations do not themselves cause exit 1.

## Formats

| Input | Handling |
|---|---|
| Markdown, text, extensionless text | UTF-8/LF; headings, lists, code and text retained |
| Diplodoc HTML | Parse `diplodoc-state`, decode article HTML, use MarkItDown; structured landing-page data supported |
| Ordinary HTML | Prefer article/main; remove navigation, scripts/styles; retain tables, links and explicit anchors |
| DOCX | MarkItDown local DOCX converter for paragraphs, headings, lists and tables |
| XLSX | Focused openpyxl adapter: every worksheet, populated cells, cached values or unevaluated formulas |
| CSV | Sniff delimiter, render table; preserve ragged rows with padding and a warning |
| Architecture JSON | Observed schema-v1 entities, components, relations, scenarios and all remaining fields in readable sections |
| Other JSON/YAML | Labeled fences; malformed JSON fails explicitly |
| Mermaid, PlantUML, shell, standalone JavaScript | Labeled fences, never executed |
| Images/SVG | Inert, hash-addressed copied assets with provenance; inline base64 images extracted |

Markdown notes and cuts become visible labeled annotations with all content retained. Local includes are expanded using their own link context; include/code directives for utilities become code fences. Unknown directives (including tabs/TOC), missing or circular includes remain visible and generate warnings. Directives and links inside fenced/inline code are not interpreted.

UTF-8 and BOM-marked UTF-16/32 are supported. Inferred legacy encoding produces a partial status for manual verification. Unknown binary formats, system files, CSS/generated `_bundle` resources, archives and `llms-full.txt` are explicitly skipped. The output subtree is outside the scan universe.

## Output and traceability

```text
normalized/
  documents/<stable-id>/content.md
  documents/<stable-id>/provenance.json
  assets/<sha256>.<extension>
  assets/provenance.json
  manifest.jsonl
  llms.txt
  report.json
  roots.json
  .docnorm-owned.json
```

The manifest has one record for every encountered input file/symlink, including hashes, explicit status, output, warnings and errors. `converted`, `partial`, `merged`, `asset`, `skipped` and `failed` are distinct. For merged files, provenance retains the original conversion outcome—even if an empty rendered representation failed and raw Markdown supplied the document. The report separates file-status counts from document totals and includes source-specific warnings, errors and exclusion reasons.

Provenance contains canonical/selected sources, all source roles and hashes, converter versions, include extraction inputs, metadata and available locators. It does **not** invent line mappings. To relocate originals, edit the `source.path` entry in `roots.json`; all provenance paths remain relative to that root.

Source metadata and local Markdown-alternate links associate rendered/raw representations; ambiguous matches are not merged. Usable rendered content wins, then raw Markdown. Byte-identical documents merge by SHA-256, never by title. IDs hash the canonical relative source identity rather than contents or the absolute folder location. Adding/removing representations can change a group's canonical identity; editing an existing unique source does not.

Resolvable local document/asset links are rewritten relative to `content.md`; external links remain unchanged. Original HTML anchors are preserved, and standard Markdown heading anchors are recognized. Missing destinations/fragments remain visible and are reported. `llms.txt` contains working relative document and asset links, not summaries.

## Safety and reruns

Inputs are read-only. Symlinks are never followed, including includes and output destinations. Includes cannot escape the input root and have a 64-level nesting cap. Office archives have a 256 MiB unpacked-size limit. Python socket connections/DNS are blocked during normalization. Only local MarkItDown HTML/DOCX converters are called, with no plugins, remote dispatcher or LLM client; ONNX telemetry is disabled before importing MarkItDown. Its dependency initializer can emit a platform diagnostic on macOS.

Outputs are staged, each file is atomically replaced, and collection metadata is published last. An ownership journal allows retry after interrupted publication. Only stale paths recorded as tool-owned can be deleted; unrelated files survive, and unowned filename collisions fail without overwriting them. Publication is **file-atomic, not a simultaneous whole-directory snapshot**. Do not run concurrent writers against one output directory or edit ownership files by hand.

## Tests and export validation

```sh
.venv/bin/pytest -q
.venv/bin/python scripts/validate_collection.py
```

Tests block networking and cover formats, Unicode, encoding, tables, warnings, duplicate matching, broken links, malformed/empty input, include cycles/traversal, symlinks, interruption recovery and stale cleanup. The validation script checks the bundled source-hash baseline, provenance, assets, catalog/rewritten links and byte-identical reruns; it writes `validation-report.json`.

The actual export's report is `../normalized/report.json`. Failed empty pages and unresolved links are retained in that report rather than disguised as successful conversions. The architecture export includes JavaScript-only HTML shells: these cannot yield article text without executing code and are reported as empty; their model is retained via `architecture.json`. Several Diplodoc/raw pairs are genuinely empty. Other rendered failures may be covered by raw-source fallback.

Delivered run: **1,951 scanned files → 789 normalized documents and 245 deduplicated assets**. File statuses: 733 converted, 56 partial, 610 merged, 461 asset sources, 72 skipped and 19 failed. **26 tests pass**. Validation confirms unchanged source hashes, valid provenance, all 1,034 catalog links and 1,592 rewritten links, 414 DOCX text runs, all three XLSX worksheets, and byte-identical reruns. See `validation-report.json` for the machine-readable evidence.

## Known limitations

- Not a lossless layout converter: Office formatting, charts, macros, embedded objects and interactive HTML behavior are not rendered. Spreadsheet merged-cell presentation is flattened; first CSV row is treated as its header, while XLSX receives generated column labels so every original row remains data.
- Unknown Diplodoc directives are preserved and reported, not fully rendered. Include fragments and heading-slug conventions outside the supported syntax may require manual fixes.
- Static Markdown anchor conventions vary across viewers; explicit HTML anchors are the most reliable. Broken source links are not guessed or fetched.
- Encodings without a BOM may need manual review. Content-based duplicate merging selects one representation's local link context; source references remain in provenance.
- No SQLite/search backend, chunking, embeddings, remote fetching, watchers, OCR, image interpretation or generated summaries.
