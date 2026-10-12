"""Validate paths and emit wrapper state; normalization/search stay in existing CLIs."""
import hashlib
import json
import os
from pathlib import Path
import sys
from datetime import datetime, timezone


def checked(path):
    raw = Path(path).absolute()
    if any(p.is_symlink() for p in (raw, *raw.parents)):
        raise ValueError(f"symlink path components are forbidden: {raw}")
    path = Path(os.path.abspath(raw))
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError(f"symlink path components are forbidden: {path}")
    return path


def main():
    command, root_arg, input_arg = sys.argv[1:4]
    root = checked(root_arg)
    source = checked(input_arg)
    runtime = checked(root / "LLM-docs" / "runtime")
    if not source.is_dir():
        raise ValueError(f"input must be an existing export directory: {source}")
    if source == runtime or source in runtime.parents or runtime in source.parents:
        raise ValueError("input and LLM-docs/runtime must not overlap")
    # Bind state to both absolute locations. A moved bundle must never reuse
    # an index whose normalized-root metadata points to its old directory.
    key = hashlib.sha256(os.fsencode(str(root)) + b"\0" + os.fsencode(str(source))).hexdigest()
    state = checked(runtime / key)
    # Inspect managed destinations before any command can write through them.
    for name in ("normalized", "search.sqlite", "normalize-summary.json", "index-report.json", "ready.json", ".lock"):
        checked(state / name)
    checked(state / "normalized" / "documents")
    if command == "locate":
        print(state)
    elif command == "ready":
        norm = json.loads((state / "normalize-summary.json").read_text())
        index = json.loads((state / "index-report.json").read_text())
        report = {
            "input": str(source), "state": str(state),
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "normalization_exit": int(sys.argv[4]), "index_exit": int(sys.argv[5]),
            "normalization": norm, "index": index,
            "normalization_report": str(state / "normalized" / "report.json"),
        }
        tmp = state / "ready.tmp"
        # Exclusive creation avoids overwriting a leftover/unowned temporary file.
        with tmp.open("x", encoding="utf-8") as out:
            json.dump(report, out, ensure_ascii=False, indent=2)
            out.write("\n")
        tmp.replace(state / "ready.json")
        print(json.dumps(report, ensure_ascii=False))
    elif command == "status":
        report = json.loads((state / "ready.json").read_text())
        if report["input"] != str(source):
            raise ValueError("state belongs to another input")
        print(json.dumps(report, ensure_ascii=False))
    else:
        raise ValueError("unknown state operation")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError) as exc:
        print(f"local-docs: {exc}", file=sys.stderr)
        raise SystemExit(2)
