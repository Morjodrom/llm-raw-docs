#!/usr/bin/env bash
# Source from Bash 3.2+. An override is one executable, never a shell command.
select_local_docs_python() {
    if [[ -n ${LOCAL_DOCS_PYTHON:-} ]]; then
        LOCAL_DOCS_SELECTED=$LOCAL_DOCS_PYTHON
    elif command -v python3.13 >/dev/null 2>&1; then
        LOCAL_DOCS_SELECTED=$(command -v python3.13)
    elif command -v python3 >/dev/null 2>&1; then
        LOCAL_DOCS_SELECTED=$(command -v python3)
    else
        printf 'local-docs: install CPython 3.13 with venv/pip and SQLite FTS5 first\n' >&2
        return 2
    fi
    "$LOCAL_DOCS_SELECTED" -I -c '
import platform, sqlite3, sys, sysconfig
if sys.implementation.name != "cpython" or sys.version_info[:2] != (3, 13) or sysconfig.get_config_var("Py_GIL_DISABLED"):
    sys.exit("local-docs: requires ordinary (GIL-enabled) CPython 3.13; select it with LOCAL_DOCS_PYTHON")
if platform.system() != "Darwin" or platform.machine() != "arm64" or int(platform.mac_ver()[0].split(".")[0]) < 14:
    sys.exit("local-docs: this ZIP requires macOS 14+ ARM64 (Apple Silicon)")
try:
    import venv, ensurepip
    sqlite3.connect(":memory:").execute("CREATE VIRTUAL TABLE probe USING fts5(text)")
except Exception as exc:
    sys.exit("local-docs: requires venv/ensurepip and SQLite FTS5: " + str(exc))
' || return 2
}
