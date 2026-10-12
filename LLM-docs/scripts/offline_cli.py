"""Run the existing CLIs with Python networking disabled; never install anything."""
import os
from pathlib import Path
import runpy
import socket
import sys


def denied(*args, **kwargs):
    raise RuntimeError("LLM-docs runtime network access is forbidden")


socket.socket.connect = denied
socket.socket.connect_ex = denied
socket.create_connection = denied
socket.getaddrinfo = denied
socket.gethostbyname = denied
socket.gethostbyname_ex = denied
socket.gethostbyaddr = denied
os.environ["ORT_DISABLE_TELEMETRY"] = "1"
root = Path(__file__).resolve().parents[2]
tool, *args = sys.argv[1:]
if tool == "docnorm":
    sys.path.insert(0, str(root / "docnorm" / "src"))
    sys.argv = ["docnorm", *args]
    runpy.run_module("docnorm", run_name="__main__")
elif tool == "docsearch":
    sys.path.insert(0, str(root / "docsearch"))
    sys.argv = ["docsearch", *args]
    runpy.run_path(str(root / "docsearch" / "cli.py"), run_name="__main__")
else:
    raise SystemExit("Unknown local tool")
