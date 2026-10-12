"""Local-only filesystem and network boundaries."""
from contextlib import contextmanager
from pathlib import Path
import socket
from unittest.mock import patch


def safe_path(root: Path, path: Path) -> Path:
    # Check components before resolving: resolving first would hide symlinks.
    path = Path(__import__("os").path.abspath(path))
    try:
        relative = path.relative_to(root)
    except ValueError:
        raise ValueError("path is outside input root") from None
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise ValueError("symlink access is forbidden")
    return path


@contextmanager
def offline():
    def denied(*args, **kwargs):
        raise RuntimeError("network access is forbidden during normalization")

    with patch.object(socket.socket, "connect", denied), patch.object(
        socket.socket, "connect_ex", denied
    ), patch.object(socket, "create_connection", denied), patch.object(
        socket, "getaddrinfo", denied
    ):
        yield


def validate_roots(source: Path, output: Path):
    for path in (source, output):
        for parent in (path, *path.parents):
            if parent.is_symlink():
                raise ValueError("source/output path must not contain symlinks")
    if not source.is_dir():
        raise ValueError("source must be an existing directory")
    if output == source or output in source.parents:
        raise ValueError("output must not equal or contain the input directory")
    if output.exists() and not output.is_dir():
        raise ValueError("output must be a directory")
