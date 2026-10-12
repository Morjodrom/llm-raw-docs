"""Measured export acceptance run; refuses any source hash/path change."""
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "LLM-docs/local-docs.sh"


def source_snapshot(source):
    values = {}
    for folder, dirs, files in os.walk(source, followlinks=False):
        for name in dirs + files:
            path = Path(folder) / name
            relative = path.relative_to(source).as_posix()
            if path.is_symlink():
                values[relative] = {"symlink": os.readlink(path)}
            elif path.is_file():
                values[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
            else:
                values[relative] = {"directory": True}
    return values


def run(command, source, *args):
    start = time.perf_counter()
    result = subprocess.run([str(SCRIPT), command, "--input", str(source), *args],
                            text=True, capture_output=True, cwd=ROOT)
    elapsed = time.perf_counter() - start
    if result.returncode not in ((0, 1) if command == "update" else (0,)):
        raise RuntimeError(f"{command}: exit {result.returncode}: {result.stderr}")
    return json.loads(result.stdout), result.returncode, elapsed


def main():
    source = Path(sys.argv[1]).absolute()
    before = source_snapshot(source)
    first, first_code, first_seconds = run("update", source)
    state = Path(first["state"])
    normalized = state / "normalized"
    report = json.loads((normalized / "report.json").read_text())
    connection = sqlite3.connect(f"{(state / 'search.sqlite').as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    documents = connection.execute("SELECT * FROM documents").fetchall()
    sections = connection.execute("SELECT * FROM sections").fetchall()
    indexed_provenance = {}
    source_lines = {}
    roots = json.loads((normalized / "roots.json").read_text())
    for doc in documents:
        provenance = json.loads(doc["provenance"])
        path = normalized / doc["relative_path"]
        expected = json.loads(path.with_name("provenance.json").read_text())
        assert provenance == dict(expected, roots=roots), doc["relative_path"]
        indexed_provenance[doc["document_id"]] = provenance
        with path.open(encoding="utf-8", newline="") as handle:
            text = handle.read()
        source_lines[doc["document_id"]] = [m.group() for m in re.finditer(r"[^\r\n]*(?:\r\n|\r|\n|$)", text) if m.group()]
    for section in sections:
        lines = source_lines[section["document_id"]]
        assert section["original_markdown"] == "".join(lines[section["start_line"]-1:section["end_line"]])
    connection.close()
    search, _, search_seconds = run("search", source, "ошибки подключения", "--limit", "5")
    assert search["results"], "acceptance query unexpectedly returned no hits"
    hit = search["results"][0]
    read, _, read_seconds = run("read", source, hit["section_id"])
    assert read["section_id"] == hit["section_id"]
    assert read["provenance"] == indexed_provenance[read["document_id"]]
    assert read["original_markdown"]
    assert not read["stale"]
    parent = None
    if read["parent_section_id"]:
        parent, _, _ = run("read", source, read["parent_section_id"])
        assert parent["document_id"] == read["document_id"]
    literal, _, _ = run("search", source, "--mode", "literal", "--", "--db")
    advanced, _, _ = run("search", source, "body:подключения OR heading:ошибки", "--mode", "advanced")
    any_query, _, _ = run("search", source, "подключение таймаут", "--match", "any")
    second, second_code, second_seconds = run("update", source)
    assert second["index"]["indexed"] == 0
    assert second["index"]["unchanged"] == len(documents)
    assert second["index"]["metadata_updated"] == 0
    assert second["index"]["failures"] == []
    after = source_snapshot(source)
    assert before == after, "Source export contents or paths changed"
    value = {
        "verified_at": datetime.now(timezone.utc).isoformat(), "input": str(source),
        "source_files_hashed": sum(isinstance(v, str) for v in before.values()),
        "source_unchanged": True, "documents": len(documents), "sections": len(sections),
        "all_section_slices_verified": True, "all_document_provenance_verified": True,
        "normalization_file_statuses": report["file_status_counts"],
        "normalization_partial_documents": report["partial_documents"],
        "first_update_exit": first_code, "rerun_update_exit": second_code,
        "first_update_seconds": round(first_seconds, 3),
        "rerun_update_seconds": round(second_seconds, 3), "rerun_index": second["index"],
        "query": search["query"], "query_result_count": len(search["results"]),
        "search_seconds": round(search_seconds, 3), "read_seconds": round(read_seconds, 3),
        "retrieved_section": {k: read[k] for k in ("section_id", "title", "heading_path", "relative_path", "start_line", "end_line", "stale")},
        "parent_retrieved": parent is not None,
        "additional_mode_results": {"literal_dash_query": len(literal["results"]), "advanced": len(advanced["results"]), "plain_any": len(any_query["results"])},
        "runtime_network_guard": "actual launcher probe tested in test_workflow.py; not an OS sandbox",
        "skill_validation": "quick_validate.py unavailable: PyYAML missing in tool and bundled environments; manually checked frontmatter, naming, links, and scope",
        "native_skill_discovery": "relative project skill link verified; automatic client inventory reload not tested",
    }
    target = ROOT / "LLM-docs/test-results.json"
    target.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(value, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
