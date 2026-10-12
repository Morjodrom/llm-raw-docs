"""Real wrapper integration: immutable sources, isolation, updates, errors, offline guard."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "LLM-docs" / "local-docs.sh"


def invoke(command, source, *args, env=None, cwd=None):
    return subprocess.run([str(SCRIPT), command, "--input", str(source), *args],
                          capture_output=True, text=True, env=env, cwd=cwd or ROOT)


@pytest.fixture
def source():
    # Real directory, spaces/non-ASCII and no symlinked /tmp components.
    with tempfile.TemporaryDirectory(prefix="docs тест ", dir="/private/tmp") as folder:
        path = Path(folder)
        yield path
        key = hashlib.sha256(os.fsencode(str(path))).hexdigest()
        shutil.rmtree(ROOT / "LLM-docs" / "runtime" / key, ignore_errors=True)


def update(source):
    result = invoke("update", source)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_search_retrieve_provenance_and_unchanged_rerun(source):
    body = "# Сеть\n\n## Ошибки\nОшибка подключения сервера. Параметр --db.\n"
    (source / "guide.md").write_text(body)
    report = update(source)
    result = invoke("search", source, "ошибки подключения", "--limit", "5", cwd="/private/tmp")
    assert result.returncode == 0, result.stderr
    hit = json.loads(result.stdout)["results"][0]
    read = invoke("read", source, hit["section_id"])
    section = json.loads(read.stdout)
    assert read.returncode == 0
    assert "Ошибка подключения" in section["original_markdown"]
    assert section["provenance"]["selected_source"] == "guide.md"
    assert section["parent_section_id"]
    parent = invoke("read", source, section["parent_section_id"])
    assert parent.returncode == 0
    assert not section["stale"]
    assert (source / "guide.md").read_text() == body
    rerun = update(source)
    assert rerun["index"]["unchanged"] == 1
    assert rerun["index"]["indexed"] == 0
    assert report["state"] == rerun["state"]
    literal = invoke("search", source, "--mode", "literal", "--", "--db")
    assert literal.returncode == 0, literal.stderr
    assert json.loads(literal.stdout)["results"]


def test_edits_and_removals_do_not_retain_old_hits(source):
    guide = source / "guide.md"
    guide.write_text("# Guide\nOldUniqueNeedle\n")
    update(source)
    guide.write_text("# Guide\nNewUniqueNeedle\n")
    update(source)
    assert not json.loads(invoke("search", source, "OldUniqueNeedle").stdout)["results"]
    assert json.loads(invoke("search", source, "NewUniqueNeedle").stdout)["results"]
    guide.unlink()
    report = update(source)
    assert report["index"]["deleted"] == 1
    assert not json.loads(invoke("search", source, "NewUniqueNeedle").stdout)["results"]


def test_partial_conversion_remains_visible_and_searchable(source):
    (source / "good.md").write_text("# Guide\nVisibleNeedle\n")
    (source / "bad.json").write_text("{broken")
    result = invoke("update", source)
    assert result.returncode == 1, result.stderr
    report = json.loads(result.stdout)
    assert report["normalization_exit"] == 1
    assert report["normalization"]["files"]["failed"] == 1
    assert invoke("status", source).returncode == 0
    assert json.loads(invoke("search", source, "VisibleNeedle").stdout)["results"]


def test_missing_snapshot_invalid_query_and_missing_section(source):
    assert invoke("search", source, "needle").returncode == 2
    (source / "guide.md").write_text("# Guide\nNeedle\n")
    update(source)
    assert invoke("search", source, "--db", "/private/tmp/escape.sqlite", "needle").returncode == 2
    assert invoke("search", source, "title:(", "--mode", "advanced").returncode == 2
    assert invoke("read", source, "nonexistent-section").returncode == 3
    assert json.loads(invoke("search", source, "NoMatchUnique").stdout)["results"] == []


def test_lock_and_fatal_refresh_gate_queries(source):
    (source / "guide.md").write_text("# Guide\nNeedle\n")
    state = Path(update(source)["state"])
    (state / ".lock").mkdir()
    assert invoke("search", source, "Needle").returncode == 2
    assert invoke("update", source).returncode == 2
    (state / ".lock").rmdir()
    # An executable that fails models a fatal CLI dependency/setup error.
    fail = source / "fail-python"
    fail.write_text("#!/bin/sh\nexit 2\n")
    fail.chmod(0o755)
    env = dict(os.environ, DOCNORM_PYTHON=str(fail))
    assert invoke("update", source, env=env).returncode == 2
    assert not (state / "ready.json").exists()
    assert invoke("search", source, "Needle").returncode == 2
    assert not (state / ".lock").exists()


def test_distinct_exports_symlinks_and_overlap(source):
    (source / "guide.md").write_text("# Guide\nFirstNeedle\n")
    first = update(source)
    with tempfile.TemporaryDirectory(dir="/private/tmp") as folder:
        other = Path(folder)
        (other / "guide.md").write_text("# Guide\nSecondNeedle\n")
        second = update(other)
        try:
            assert first["state"] != second["state"]
            assert not json.loads(invoke("search", other, "FirstNeedle").stdout)["results"]
        finally:
            shutil.rmtree(second["state"])
    (source / "alias").symlink_to(source, target_is_directory=True)
    assert invoke("update", source / "alias").returncode == 2
    assert invoke("update", ROOT).returncode == 2
    state = Path(first["state"])
    (state / "ready.json").unlink()
    (state / "ready.json").symlink_to(source / "guide.md")
    assert invoke("update", source).returncode == 2
    assert (source / "guide.md").read_text() == "# Guide\nFirstNeedle\n"


def test_offline_guard_blocks_network_before_cli_import():
    # Execute the actual launcher with a probe injected via runpy, not a second guard.
    code = '''
import runpy, socket, sys
from unittest.mock import patch
def probe(*args, **kwargs):
    for action in (lambda: socket.create_connection(("example.com", 443)),
                   lambda: socket.getaddrinfo("example.com", 443),
                   lambda: socket.socket().connect(("127.0.0.1", 80))):
        try:
            action()
        except RuntimeError as exc:
            assert "network access is forbidden" in str(exc)
        else:
            raise AssertionError("network guard absent")
sys.argv = ["offline_cli.py", "docsearch", "--help"]
with patch.object(runpy, "run_path", probe):
    exec(compile(open(sys.argv_original).read(), sys.argv_original, "exec"), {"__file__": sys.argv_original})
'''
    # Avoid relying on startup environment tricks that the wrapper deliberately clears.
    code = code.replace('sys.argv_original', repr(str(ROOT / 'LLM-docs/scripts/offline_cli.py')))
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_project_skill_link_resolves_to_source():
    link = ROOT / ".agents/skills/local-docs-search"
    assert link.is_symlink()
    assert (link / "SKILL.md").resolve() == ROOT / "LLM-docs/skills/local-docs-search/SKILL.md"
