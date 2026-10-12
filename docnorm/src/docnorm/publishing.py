"""Atomic files and an ownership journal for interruption-safe reruns."""
import json
import os
from pathlib import Path, PurePosixPath
import tempfile


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()


def checked_destination(output, relative):
    path = PurePosixPath(relative)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValueError("invalid owned output path")
    target = output.joinpath(*path.parts)
    for p in (target, *target.parents):
        if p == output.parent:
            break
        if p.is_symlink():
            raise ValueError(f"symlink output destination: {relative}")
    return target


def atomic_file(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".docnorm-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def publish(output: Path, files: dict[str, bytes]):
    ownership = checked_destination(output, ".docnorm-owned.json")
    journal = checked_destination(output, ".docnorm-transaction.json")
    previous = set()
    for state in (ownership, journal):
        if state.exists():
            obj = json.loads(state.read_text())
            if obj.get("tool") != "docnorm" or obj.get("schema_version") != 1:
                raise ValueError("unrecognized output ownership metadata")
            for relative in obj["paths"]:
                checked_destination(output, relative)
                previous.add(relative)
    current = set(files)
    for relative in current:
        target = checked_destination(output, relative)
        if target.exists() and relative not in previous:
            raise ValueError(f"refusing to overwrite unowned output file: {relative}")
        if target.exists() and not target.is_file():
            raise ValueError(f"output destination is not a file: {relative}")
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".docnorm-stage-", dir=output) as staging:
        stage = Path(staging)
        for relative, data in sorted(files.items()):
            path = stage / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        # Journal proves ownership of new files if publication is interrupted.
        atomic_file(journal, json_bytes({"tool": "docnorm", "schema_version": 1, "paths": sorted(previous | current)}))
        metadata = {"manifest.jsonl", "llms.txt", "report.json", "roots.json", "assets/provenance.json"}
        for relative in sorted(files, key=lambda p: (p in metadata, p)):
            target = checked_destination(output, relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(stage / relative, target)
        for relative in sorted(previous - current):
            target = checked_destination(output, relative)
            if target.is_file():
                target.unlink()
                # Remove empty tool directories only, never recursive deletion.
                parent = target.parent
                while parent != output:
                    try:
                        parent.rmdir()
                    except OSError:
                        break
                    parent = parent.parent
        atomic_file(ownership, json_bytes({"tool": "docnorm", "schema_version": 1, "paths": sorted(current)}))
        journal.unlink()
