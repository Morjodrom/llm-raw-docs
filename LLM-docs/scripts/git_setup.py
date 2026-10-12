"""Explicit one-command Git provisioning; --check is offline and never installs."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import venv

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "distribution"))
from provision import checked, pip_env, sha, validate_environment

OWNER = "llm-raw-docs-git-venv-v1"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--offline", action="store_true", help="Install only from locally provided wheels; never use an index")
    parser.add_argument("--wheelhouse", type=Path, help="Optional local wheel directory")
    args = parser.parse_args()
    root = checked(args.root.absolute())
    state = checked(root / ".venv")
    ready = checked(state / "local-docs-ready.json")
    owner = checked(state / ".local-docs-owner")
    python = state / "bin/python"
    identity = {"root": str(root), "python": os.path.realpath(sys.executable), "version": sys.version,
                "requirements": sha(checked(root / "requirements.txt")),
                "lock": sha(checked(root / "LLM-docs/distribution/requirements.lock"))}
    previous = None
    if ready.is_file():
        try:
            previous = json.loads(ready.read_text())
        except ValueError:
            pass
    owned = owner.is_file() and owner.read_text() == OWNER
    if args.check:
        if not owned or previous != identity or not python.is_file():
            raise ValueError("Git environment missing, incomplete, moved or dependencies changed; run bash setup.sh")
        if (root / ".local-docs-setup.lock").exists():
            raise ValueError("setup is running/interrupted; do not use the environment until setup completes")
        print(python)
        return
    lock = checked(root / ".local-docs-setup.lock")
    try:
        lock.mkdir()
    except FileExistsError:
        raise ValueError(f"setup busy/interrupted: {lock}; confirm no installer is running before rmdir of the empty lock")
    try:
        if state.exists() and not owned:
            raise ValueError(f"refusing to overwrite unowned environment: {state}; move it aside or use a fresh clone")
        if previous == identity and owned and python.is_file():
            print("local-docs: already set up. Run ./local-docs.sh or ask your assistant.")
            return
        if state.exists():
            shutil.rmtree(state)  # Only our marked environment; never source exports.
        state.mkdir()
        owner.write_text(OWNER)
        venv.EnvBuilder(with_pip=True).create(state)
        env = pip_env()
        if not args.offline:
            env.pop("PIP_NO_INDEX")
        command = [str(python), "-I", "-m", "pip", "install", "--no-cache-dir", "--only-binary=:all:"]
        if args.offline:
            command.append("--no-index")
        if args.wheelhouse:
            command.extend(["--find-links", str(args.wheelhouse.resolve())])
        command.extend(["-r", str(root / "requirements.txt")])
        subprocess.run(command, check=True, env=env, stdout=sys.stderr)
        subprocess.run([str(python), "-I", "-m", "pip", "check"], check=True, env=pip_env(), stdout=sys.stderr)
        validate_environment(python, root)
        temporary = checked(state / "local-docs-ready.tmp")
        with temporary.open("x", encoding="utf-8") as out:
            json.dump(identity, out, sort_keys=True)
        temporary.replace(ready)
        print("local-docs: ready. Run ./local-docs.sh or ask your assistant. No activation or exports needed.")
    finally:
        lock.rmdir()


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"local-docs: {exc}", file=sys.stderr)
        raise SystemExit(2)
