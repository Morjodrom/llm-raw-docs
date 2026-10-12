"""Acceptance against the actual ZIP, not the developer virtual environments."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[2]
ARCHIVE = ROOT / "LLM-docs/dist/local-docs-macos-arm64-py313.zip"


def run(bundle, command, source, *args, **kwargs):
    env = dict(os.environ)
    for name in ("DOCNORM_PYTHON", "DOCSEARCH_PYTHON", "PYTHONPATH"):
        env.pop(name, None)
    # Deliberately unusable indexes: provisioning must never consult them.
    env.update(PIP_INDEX_URL="http://127.0.0.1:1", PIP_EXTRA_INDEX_URL="http://127.0.0.1:1")
    env.update(kwargs.pop("env", {}))
    return subprocess.run(["bash", str(bundle / "local-docs.sh"), command, "--input", str(source), *args],
                          capture_output=True, text=True, cwd="/private/tmp", env=env, **kwargs)


@pytest.fixture(scope="module")
def installed():
    with tempfile.TemporaryDirectory(prefix="distribution тест ", dir="/private/tmp") as tmp:
        folder = Path(tmp)
        with zipfile.ZipFile(ARCHIVE) as archive:
            for info in archive.infolist():
                assert info.filename.startswith("local-docs/") and ".." not in Path(info.filename).parts
            archive.extractall(folder)
            for info in archive.infolist():
                (folder / info.filename).chmod((info.external_attr >> 16) & 0o777)
        bundle = folder / "local-docs"
        source = folder / "сырой export"
        source.mkdir()
        (source / "guide.md").write_text("# Сеть\n\n## Ошибки\nОшибка подключения сервера.\n")
        assert not (bundle / ".local-docs-env").exists()
        first = run(bundle, "update", source)
        assert first.returncode == 0, first.stderr
        assert "provisioning offline environment" in first.stderr
        assert not (bundle / "docnorm/.venv").exists()
        assert not (bundle / "docsearch/.venv").exists()
        assert (bundle / ".local-docs-env/ready.json").is_file()
        yield bundle, source, folder


def test_archive_manifest_and_exclusions(installed):
    bundle, source, _ = installed
    manifest = json.loads((bundle / "manifest.json").read_text())
    for name, digest in manifest["files"].items():
        assert hashlib.sha256((bundle / name).read_bytes()).hexdigest() == digest
        assert not any(part in {".venv", "__pycache__", "runtime", "artifacts", "normalized"} for part in Path(name).parts)
        assert not name.endswith((".sqlite", ".db"))
        assert "drive-download" not in name
    skill = bundle / ".agents/skills/local-docs-search/SKILL.md"
    assert skill.is_file() and not skill.is_symlink()
    assert (skill.parent / "../../../LLM-docs/README.md").resolve().is_file()
    assert any("pymorphy3_dicts_ru" in name for name in manifest["files"])


def test_formats_search_read_incremental_immutable(installed):
    bundle, source, _ = installed
    fixture_code = '''
from pathlib import Path
import sys
import zipfile
from openpyxl import Workbook
source = Path(sys.argv[1])
with zipfile.ZipFile(source/"office.docx", "w") as z:
    z.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
    z.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>DocxUniqueNeedle</w:t></w:r></w:p></w:body></w:document>')
w = Workbook(); w.active.append(["XlsxUniqueNeedle", 42]); w.save(source/"table.xlsx")
'''
    subprocess.run([str(bundle / ".local-docs-env/bin/python"), "-c", fixture_code, str(source)], check=True)
    (source / "web.html").write_text("<html><body><h1>Web</h1><p>HtmlUniqueNeedle</p></body></html>")
    before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in source.iterdir()}
    update = run(bundle, "update", source)
    assert update.returncode == 0, update.stderr
    assert "provisioning offline environment" not in update.stderr
    for query in ("ошибки подключения", "DocxUniqueNeedle", "XlsxUniqueNeedle", "HtmlUniqueNeedle"):
        result = run(bundle, "search", source, query)
        assert result.returncode == 0, result.stderr
        hit = json.loads(result.stdout)["results"][0]
        read = run(bundle, "read", source, hit["section_id"])
        assert read.returncode == 0
        section = json.loads(read.stdout)
        assert section["original_markdown"] and section["provenance"] and not section["stale"]
        if query.startswith("ошибки"):
            assert "Ошибка подключения" in section["original_markdown"]
            assert section["parent_section_id"]
            assert run(bundle, "read", source, section["parent_section_id"]).returncode == 0
    assert before == {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in source.iterdir()}
    unchanged = run(bundle, "update", source)
    assert json.loads(unchanged.stdout)["index"]["unchanged"] == 4
    guide = source / "guide.md"
    guide.write_text("# Сеть\nEditedUniqueNeedle\n")
    assert run(bundle, "update", source).returncode == 0
    assert not json.loads(run(bundle, "search", source, "ошибки подключения").stdout)["results"]
    assert json.loads(run(bundle, "search", source, "EditedUniqueNeedle").stdout)["results"]
    guide.unlink()
    deleted = run(bundle, "update", source)
    assert json.loads(deleted.stdout)["index"]["deleted"] == 1
    assert run(bundle, "read", source, "missing").returncode == 3


def test_partial_fatal_and_locks(installed):
    bundle, source, _ = installed
    (source / "broken.json").write_text("{invalid")
    partial = run(bundle, "update", source)
    assert partial.returncode == 1
    assert json.loads(partial.stdout)["normalization_exit"] == 1
    assert run(bundle, "search", source, "HtmlUniqueNeedle").returncode == 0
    state = Path(json.loads(partial.stdout)["state"])
    (state / ".lock").mkdir()
    assert run(bundle, "update", source).returncode == 2
    (state / ".lock").rmdir()
    # Existing wrapper gate is tested with a fatal normalization executable.
    fail = source.parent / "fail-python"
    fail.write_text("#!/bin/sh\nexit 2\n"); fail.chmod(0o755)
    env = dict(os.environ, DOCNORM_PYTHON=str(fail), DOCSEARCH_PYTHON=str(bundle / ".local-docs-env/bin/python"))
    fatal = subprocess.run(["bash", str(bundle / "LLM-docs/local-docs.sh"), "update", "--input", str(source)], env=env, capture_output=True)
    assert fatal.returncode == 2 and not (state / "ready.json").exists()
    assert run(bundle, "search", source, "HtmlUniqueNeedle").returncode == 2
    assert run(bundle, "update", source).returncode == 1
    (source / "broken.json").unlink()


def test_provisioning_errors_and_recovery(installed):
    bundle, source, _ = installed
    inventory = json.loads((bundle / "LLM-docs/distribution/wheelhouse.json").read_text())
    wheel = bundle / "LLM-docs/distribution/wheelhouse" / next(iter(inventory))
    original = wheel.read_bytes()
    try:
        wheel.write_bytes(b"broken")
        broken = run(bundle, "status", source)
        assert broken.returncode == 2 and "corrupted wheel" in broken.stderr
    finally:
        wheel.write_bytes(original)
    lock = bundle / ".local-docs-install.lock"
    lock.mkdir()
    busy = run(bundle, "status", source)
    assert busy.returncode == 2 and "busy/interrupted" in busy.stderr
    lock.rmdir()
    state = bundle / ".local-docs-env"
    (state / "ready.json").write_text("{truncated")  # Simulate interrupted readiness publication.
    recovered = run(bundle, "status", source)
    assert recovered.returncode == 0 and "provisioning offline environment" in recovered.stderr
    hashed = bundle / "LLM-docs/distribution/requirements.hashed.txt"
    hashed.write_text(hashed.read_text() + "# dependency lock refresh\n")
    refreshed = run(bundle, "status", source)
    assert refreshed.returncode == 0 and "provisioning offline environment" in refreshed.stderr
    spec = importlib.util.spec_from_file_location("provision_test", bundle / "LLM-docs/distribution/provision.py")
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    # Test FTS5 failure on the actual launcher check body, without changing Python.
    checker = (bundle / "LLM-docs/distribution/python.sh").read_text()
    body = checker.split(' -I -c \'', 1)[1].rsplit("' || return 2", 1)[0]
    code = "import sqlite3\ndef unavailable(*args): raise RuntimeError('FTS5 unavailable')\nsqlite3.connect=unavailable\n" + body
    probe = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert probe.returncode != 0 and "SQLite FTS5" in probe.stderr
    probe = subprocess.run([sys.executable, "-c", "import sys; sys.version_info=(3, 12)\n" + body], capture_output=True, text=True)
    assert probe.returncode != 0 and "CPython 3.13" in probe.stderr
    probe = subprocess.run([sys.executable, "-c", "import platform; platform.system=lambda: 'Linux'\n" + body], capture_output=True, text=True)
    assert probe.returncode != 0 and "macOS 14+ ARM64" in probe.stderr
    shim = source.parent / "incompatible-python"
    shim.write_text("#!/bin/sh\necho 'local-docs: requires CPython 3.13' >&2\nexit 1\n"); shim.chmod(0o755)
    incompatible = run(bundle, "status", source, env={"LOCAL_DOCS_PYTHON": str(shim)})
    assert incompatible.returncode == 2 and "CPython 3.13" in incompatible.stderr


def test_move_source_edit_and_reproducible_rebuild(installed):
    bundle, source, folder = installed
    moved = folder / "moved bundle"
    bundle.rename(moved)
    try:
        result = run(moved, "update", source)
        assert result.returncode == 0, result.stderr
        assert "provisioning offline environment" in result.stderr
        file = moved / "docnorm/src/docnorm/__init__.py"
        file.write_text(file.read_text() + "\n# EditableDistributionProbe\n")
        (moved / "docsearch/_docsearch_new.py").write_text("# AddedModuleProbe\n")
        builds = []
        for _ in range(2):
            build = subprocess.run(["bash", str(moved / "LLM-docs/build-distribution.sh"), "--offline"], capture_output=True, text=True)
            assert build.returncode == 0, build.stderr
            builds.append(json.loads(build.stdout))
        assert builds[0]["sha256"] == builds[1]["sha256"]
        with zipfile.ZipFile(builds[0]["archive"]) as rebuilt:
            assert b"EditableDistributionProbe" in rebuilt.read("local-docs/docnorm/src/docnorm/__init__.py")
            assert b"AddedModuleProbe" in rebuilt.read("local-docs/docsearch/_docsearch_new.py")
    finally:
        moved.rename(bundle)


def test_unowned_environment_is_never_deleted(tmp_path):
    spec = importlib.util.spec_from_file_location("provision_ownership", ROOT / "LLM-docs/distribution/provision.py")
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    root = tmp_path.resolve()
    bundle = root / "LLM-docs/distribution"
    bundle.mkdir(parents=True)
    (bundle / "wheelhouse.json").write_text("{}")
    (bundle / "requirements.hashed.txt").write_text("")
    state = root / ".local-docs-env"
    state.mkdir(); keep = state / "valuable.txt"; keep.write_text("untouched")
    with pytest.raises(ValueError, match="unowned"):
        module.main(root)
    assert keep.read_text() == "untouched"
    assert not (root / ".local-docs-install.lock").exists()
