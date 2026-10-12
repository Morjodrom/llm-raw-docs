"""Clone-style setup and offline runtime; no ZIP or exported variables needed."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

import pytest

ROOT = Path(__file__).resolve().parents[2]


def copy_sources(destination):
    if (ROOT / "manifest.json").is_file():
        # The source tests are also shipped in ZIPs without Git metadata.
        files = [name for name in json.loads((ROOT / "manifest.json").read_text())["files"]
                 if not name.startswith("LLM-docs/distribution/wheelhouse")
                 and name != "LLM-docs/distribution/requirements.hashed.txt"]
    else:
        files = subprocess.check_output(["git", "ls-files"], cwd=ROOT, text=True).splitlines()
    files += ["setup.sh", "requirements.txt", "LLM-docs/scripts/git_setup.py"]
    for name in set(files):
        source = ROOT / name
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_symlink():
            target.symlink_to(os.readlink(source))
        else:
            shutil.copy2(source, target)


def invoke(root, *args):
    env = {k: v for k, v in os.environ.items() if k not in {"DOCNORM_PYTHON", "DOCSEARCH_PYTHON", "PYTHONPATH"}}
    env.update(PIP_INDEX_URL="http://127.0.0.1:1", PIP_EXTRA_INDEX_URL="http://127.0.0.1:1")
    return subprocess.run(["bash", str(root / args[0]), *args[1:]], cwd="/private/tmp",
                          capture_output=True, text=True, env=env)


@pytest.fixture(scope="module")
def clone():
    with tempfile.TemporaryDirectory(prefix="git setup тест ", dir="/private/tmp") as tmp:
        folder = Path(tmp)
        root = folder / "clone"
        root.mkdir()
        copy_sources(root)
        assert not (root / "LLM-docs/distribution/wheelhouse").exists()
        yield root, folder


def test_missing_setup_does_not_install_online(clone):
    root, folder = clone
    source = folder / "export"
    source.mkdir(); (source / "guide.md").write_text("# Сеть\n\n## Ошибки\nОшибка подключения сервера.\n")
    result = invoke(root, "local-docs.sh", "update", "--input", str(source))
    assert result.returncode == 2 and "bash setup.sh" in result.stderr
    assert not (root / ".venv").exists()


def test_one_command_setup_then_unactivated_runtime(clone):
    root, folder = clone
    result = invoke(root, "setup.sh", "--offline", "--wheelhouse", str(ROOT / "LLM-docs/distribution/wheelhouse"))
    assert result.returncode == 0, result.stderr
    assert "No activation or exports needed" in result.stdout
    source = folder / "export"
    original = (source / "guide.md").read_bytes()
    result = invoke(root, "local-docs.sh", "update", "--input", str(source))
    assert result.returncode == 0, result.stderr
    result = invoke(root, "local-docs.sh", "search", "--input", str(source), "ошибки подключения")
    section = json.loads(result.stdout)["results"][0]["section_id"]
    result = invoke(root, "local-docs.sh", "read", "--input", str(source), section)
    assert result.returncode == 0 and "Ошибка подключения" in json.loads(result.stdout)["original_markdown"]
    assert (source / "guide.md").read_bytes() == original
    result = invoke(root, "setup.sh", "--offline")
    assert result.returncode == 0 and "already set up" in result.stdout


def test_changed_requirements_block_runtime_until_explicit_setup(clone):
    root, folder = clone
    requirements = root / "requirements.txt"
    requirements.write_text(requirements.read_text() + "# refreshed\n")
    result = invoke(root, "local-docs.sh", "status", "--input", str(folder / "export"))
    assert result.returncode == 2 and "bash setup.sh" in result.stderr
    result = invoke(root, "setup.sh", "--offline", "--wheelhouse", str(ROOT / "LLM-docs/distribution/wheelhouse"))
    assert result.returncode == 0, result.stderr
    assert invoke(root, "local-docs.sh", "status", "--input", str(folder / "export")).returncode == 0


def test_incomplete_install_and_lock_are_explicit(clone):
    root, folder = clone
    lock = root / ".local-docs-setup.lock"
    lock.mkdir()
    assert invoke(root, "local-docs.sh", "status", "--input", str(folder / "export")).returncode == 2
    assert invoke(root, "setup.sh").returncode == 2
    lock.rmdir()
    ready = root / ".venv/local-docs-ready.json"
    ready.write_text("{partial")
    result = invoke(root, "local-docs.sh", "status", "--input", str(folder / "export"))
    assert result.returncode == 2 and "bash setup.sh" in result.stderr
    result = invoke(root, "setup.sh", "--offline", "--wheelhouse", str(ROOT / "LLM-docs/distribution/wheelhouse"))
    assert result.returncode == 0, result.stderr


def test_unowned_venv_is_preserved(clone):
    _, folder = clone
    root = folder / "unowned clone"
    root.mkdir(); copy_sources(root)
    (root / ".venv").mkdir()
    keep = root / ".venv/valuable.txt"; keep.write_text("keep")
    result = invoke(root, "setup.sh")
    assert result.returncode == 2 and "unowned" in result.stderr
    assert keep.read_text() == "keep"


def test_moved_clone_requires_setup_then_works(clone):
    root, folder = clone
    moved = folder / "moved clone"
    root.rename(moved)
    try:
        source = folder / "export"
        result = invoke(moved, "local-docs.sh", "update", "--input", str(source))
        assert result.returncode == 2 and "bash setup.sh" in result.stderr
        result = invoke(moved, "setup.sh", "--offline", "--wheelhouse", str(ROOT / "LLM-docs/distribution/wheelhouse"))
        assert result.returncode == 0, result.stderr
        assert invoke(moved, "local-docs.sh", "update", "--input", str(source)).returncode == 0
    finally:
        moved.rename(root)
