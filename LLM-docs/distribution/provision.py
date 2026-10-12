"""Local wheel provisioning, deliberately separate from the offline document CLI."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import venv

OWNER = "local-docs-environment-v1"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def pip_env():
    # Do not allow inherited indexes/configuration to change provisioning.
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PIP_", "PYTHON"))}
    env.update(PIP_CONFIG_FILE=os.devnull, PIP_NO_INDEX="1",
               PIP_DISABLE_PIP_VERSION_CHECK="1", PYTHONNOUSERSITE="1",
               PYTHONDONTWRITEBYTECODE="1", ORT_DISABLE_TELEMETRY="1")
    return env


def checked(path):
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError(f"symlink path forbidden: {path}")
    return path


def main(root):
    root = checked(root.absolute())
    bundle = root / "LLM-docs/distribution"
    lockfile = checked(bundle / "requirements.hashed.txt")
    inventory = checked(bundle / "wheelhouse.json")
    if not lockfile.is_file() or not inventory.is_file():
        raise ValueError("wheelhouse not built; run bash LLM-docs/build-distribution.sh on the build machine")
    wheels = json.loads(inventory.read_text())
    for name, digest in wheels.items():
        if Path(name).name != name or not name.endswith(".whl"):
            raise ValueError("invalid wheelhouse inventory")
        path = checked(bundle / "wheelhouse" / name)
        if not path.is_file() or sha(path) != digest:
            raise ValueError(f"missing/corrupted wheel: {name}; restore it from the ZIP")
    state = checked(root / ".local-docs-env")
    lock = checked(root / ".local-docs-install.lock")
    try:
        lock.mkdir()
    except FileExistsError:
        raise ValueError(f"provisioning busy/interrupted: {lock}; after confirming no launcher is running, remove the empty lock with rmdir")
    try:
        marker = checked(state / ".owner")
        ready = checked(state / "ready.json")
        identity = {"root": str(root), "python": os.path.realpath(sys.executable),
                    "version": sys.version, "requirements": sha(lockfile),
                    "wheels": sha(inventory)}
        if state.exists():
            if not marker.is_file() or marker.read_text() != OWNER:
                raise ValueError(f"refusing to replace unowned environment: {state}")
            try:
                previous = json.loads(ready.read_text()) if ready.is_file() else None
            except (OSError, ValueError):
                previous = None  # Interrupted readiness publication is not a usable install.
            if previous == identity and (state / "bin/python").is_file():
                print(state / "bin/python")
                return
            # Only this explicitly owned directory is disposable. Never source inputs.
            shutil.rmtree(state)
        state.mkdir()
        marker.write_text(OWNER)
        print("local-docs: first use / changed location or dependencies: provisioning offline environment", file=sys.stderr)
        venv.EnvBuilder(with_pip=True).create(state)
        python = str(state / "bin/python")
        env = pip_env()
        subprocess.run([python, "-I", "-m", "pip", "install", "--no-index", "--no-cache-dir",
                        "--only-binary=:all:", "--require-hashes", "--find-links", str(bundle / "wheelhouse"),
                        "-r", str(lockfile)], check=True, env=env, stdout=sys.stderr)
        subprocess.run([python, "-I", "-m", "pip", "check"], check=True, env=env, stdout=sys.stderr)
        probe = '''
import os, socket, sys, sqlite3
from pathlib import Path
def denied(*args, **kwargs):
    raise RuntimeError("local-docs: provisioning probe network access forbidden")
socket.socket.connect = denied
socket.socket.connect_ex = denied
socket.create_connection = denied
socket.getaddrinfo = denied
os.environ["ORT_DISABLE_TELEMETRY"] = "1"
sys.path[:0] = [str(Path(sys.argv[1])/"docnorm/src"), str(Path(sys.argv[1])/"docsearch")]
import docnorm.engine, docsearch, onnxruntime, openpyxl
onnxruntime.disable_telemetry_events()
from docnorm.adapters import local_markitdown
from _docsearch_text import TextProcessor
from _docsearch_models import SearchConfig
sqlite3.connect(":memory:").execute("CREATE VIRTUAL TABLE probe USING fts5(text)")
p = TextProcessor(SearchConfig()); assert p.lemma("ошибки") == "ошибка"; p.close()
assert "Probe" in local_markitdown(b"<html><body><h1>Probe</h1></body></html>", ".html")
'''
        subprocess.run([python, "-I", "-c", probe, str(root)], check=True, env=env, stdout=sys.stderr)
        temporary = checked(state / "ready.tmp")
        with temporary.open("x", encoding="utf-8") as out:
            out.write(json.dumps(identity, sort_keys=True))
        temporary.replace(ready)
        print(python)
    finally:
        lock.rmdir()


if __name__ == "__main__":
    try:
        main(Path(sys.argv[1]))
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"local-docs: provisioning failed: {exc}", file=sys.stderr)
        raise SystemExit(2)
