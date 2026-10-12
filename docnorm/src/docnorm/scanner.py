import hashlib
import os
from pathlib import Path

from .models import FileRecord

ASSETS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".ico"}
TEXT = {".md": "markdown", ".markdown": "markdown", ".txt": "text",
        ".html": "html", ".htm": "html", ".docx": "docx", ".xlsx": "xlsx",
        ".csv": "csv", ".json": "json", ".yaml": "yaml", ".yml": "yaml",
        ".mmd": "mermaid", ".mermaid": "mermaid", ".puml": "plantuml",
        ".plantuml": "plantuml", ".sh": "bash", ".bash": "bash", ".js": "javascript"}


def sha256(path: Path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def classify(path: Path):
    if path.name in {".DS_Store", "Thumbs.db", "desktop.ini"} or path.name.startswith("._"):
        return "excluded", "system file"
    if "__MACOSX" in path.parts:
        return "excluded", "system metadata"
    if path.name == "llms-full.txt":
        return "excluded", "aggregate duplicate catalog"
    if "_bundle" in path.parts or path.suffix.lower() == ".css":
        return "excluded", "generated presentation bundle/style"
    if path.suffix.lower() == ".zip":
        return "excluded", "archive; extracted files are the input"
    if path.suffix.lower() in ASSETS:
        return "asset", None
    if path.name == ".yfm":
        return "yaml", None
    if path.suffix.lower() in TEXT:
        return TEXT[path.suffix.lower()], None
    if not path.suffix:
        with path.open("rb") as stream:
            sample = stream.read(8192)
        if b"\x00" not in sample:
            return "text", None
        # UTF-16/32 are legitimate extensionless text encodings.
        if sample.startswith((b"\xff\xfe", b"\xfe\xff", b"\x00\x00\xfe\xff")):
            return "text", None
    return "excluded", "unsupported format"


def scan(root: Path, output: Path):
    records = []
    for directory, dirs, files in os.walk(root, followlinks=False):
        base = Path(directory)
        kept = []
        for name in sorted(dirs):
            path = base / name
            if path == output:
                # Output is deliberately outside the scan universe.
                continue
            if path.is_symlink():
                records.append(FileRecord(path.relative_to(root).as_posix(), "symlink", "skipped", "symlink directory"))
            else:
                kept.append(name)
        dirs[:] = kept
        for name in sorted(files):
            path = base / name
            rel = path.relative_to(root).as_posix()
            if path.is_symlink():
                records.append(FileRecord(rel, "symlink", "skipped", "symlink file"))
                continue
            try:
                kind, reason = classify(path)
                records.append(FileRecord(rel, kind, "skipped" if reason else "pending", reason, sha256(path)))
            except Exception as e:
                records.append(FileRecord(rel, "unknown", "failed", error=f"{type(e).__name__}: {e}"))
    return sorted(records, key=lambda r: r.path)
