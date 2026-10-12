#!/usr/bin/env bash
# Bash 3.2+ (including macOS); no network, installers, eval, or document execution.
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
NORM_PY=${DOCNORM_PYTHON:-"$ROOT/docnorm/.venv/bin/python"}
SEARCH_PY=${DOCSEARCH_PYTHON:-"$ROOT/docsearch/.venv/bin/python"}
HELPER="$ROOT/LLM-docs/scripts/workflow_state.py"
OFFLINE="$ROOT/LLM-docs/scripts/offline_cli.py"
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
state=$("$SEARCH_PY" "$HELPER" locate "$ROOT" "$input")
if [[ $command == update ]]; then
    [[ -x "$NORM_PY" ]] || fail "missing executable $NORM_PY; provision docnorm offline (see README)"
    mkdir -p -- "$state"
else
    [[ -f "$state/ready.json" && -f "$state/search.sqlite" ]] || fail "no completed snapshot; run update --input first"
fi
# Serialize both reads and writes so no wrapper query sees a half-updated collection.
mkdir -- "$state/.lock" 2>/dev/null || fail "collection busy or interrupted lock: $state/.lock (see README)"
trap 'rmdir -- "$state/.lock"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
if [[ $command == update ]]; then
    # On fatal/interrupt, old DB may remain but cannot be searched through the wrapper.
    rm -f -- "$state/ready.json"
    norm_code=0
    "$NORM_PY" "$OFFLINE" docnorm normalize "$input" --output "$state/normalized" > "$state/normalize-summary.json" || norm_code=$?
    [[ $norm_code -le 1 ]] || fail "normalization failed (exit $norm_code); index not updated"
    # docnorm omits documents/ for an empty collection; docsearch requires it.
    mkdir -p -- "$state/normalized/documents"
    index_code=0
    "$SEARCH_PY" "$OFFLINE" docsearch index "$state/normalized" --db "$state/search.sqlite" --json > "$state/index-report.json" || index_code=$?
    [[ $index_code -le 1 ]] || fail "indexing failed (exit $index_code); snapshot unavailable"
    "$SEARCH_PY" "$HELPER" ready "$ROOT" "$input" "$norm_code" "$index_code"
    if [[ $norm_code -ne 0 || $index_code -ne 0 ]]; then
        printf 'local-docs: snapshot available, but partial; inspect status and normalization_report before relying on coverage\n' >&2
        exit 1
    fi
elif [[ $command == status ]]; then
    "$SEARCH_PY" "$HELPER" status "$ROOT" "$input"
else
    "$SEARCH_PY" "$OFFLINE" docsearch "$command" --db "$state/search.sqlite" --json "$@"
fi
