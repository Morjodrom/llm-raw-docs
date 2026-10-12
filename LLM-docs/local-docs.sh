#!/usr/bin/env bash
# Bash 3.2+ (including macOS); no network, installers, eval, or document execution.
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
NORM_PY=${DOCNORM_PYTHON:-"$ROOT/docnorm/.venv/bin/python"}
SEARCH_PY=${DOCSEARCH_PYTHON:-"$ROOT/docsearch/.venv/bin/python"}
usage() {
    cat <<'EOF'
Usage: local-docs.sh COMMAND --input EXPORT_DIRECTORY [docsearch arguments]
Commands:
  update             Normalize then incrementally index; print combined JSON report.
  status             Print last completed update JSON (not a live freshness check).
  search QUERY ...   Search existing snapshot; JSON by default.
  read SECTION_ID    Retrieve complete indexed section; JSON by default.
Examples:
  LLM-docs/local-docs.sh update --input ./drive-download-20261012T111012Z-1-001
  LLM-docs/local-docs.sh search --input ./drive-download-20261012T111012Z-1-001 'ошибки подключения' --limit 5
  LLM-docs/local-docs.sh read --input ./drive-download-20261012T111012Z-1-001 SECTION_ID
Input is required, relative to the caller. Quote paths/queries; use -- before queries
starting with a dash. Options after the input are forwarded to docsearch except --db.
Runtime state is isolated by absolute input path under LLM-docs/runtime/.
Exit: 0 success, 1 completed with partial conversion/indexing, 2 setup/usage/lock
error, 3 missing section. No automatic installation or implicit update.
EOF
}
fail() { printf 'local-docs: %s\n' "$*" >&2; exit 2; }
[[ $# -gt 0 ]] || { usage >&2; exit 2; }
case "$1" in -h|--help|help) usage; exit 0 ;; esac
command=$1; shift
case "$command" in update|status|search|read) ;; *) fail "unknown command: $command" ;; esac
[[ $# -ge 2 && $1 == --input ]] || fail "expected --input EXPORT_DIRECTORY immediately after command"
input=$2; shift 2
case "$command" in
    update|status) [[ $# -eq 0 ]] || fail "unexpected arguments" ;;
    search|read) [[ $# -gt 0 ]] || fail "query/section ID required" ;;
esac
# Do not allow callers to escape the managed database binding.
for arg in "$@"; do
    [[ $arg != -- ]] || break
    case "$arg" in --db|--db=*) fail "--db is managed by this wrapper" ;; esac
done
[[ -x "$SEARCH_PY" ]] || fail "missing executable $SEARCH_PY; provision docsearch offline (see README)"
export PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1
unset PYTHONPATH PYTHONSTARTUP
if [[ $command == update ]]; then
    [[ -x "$NORM_PY" ]] || fail "missing executable $NORM_PY; provision docnorm offline (see README)"
fi
exec "$SEARCH_PY" "$ROOT/LLM-docs/scripts/workflow_run.py" "$ROOT" "$command" "$input" "$@"
