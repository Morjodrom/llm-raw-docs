#!/usr/bin/env bash
# Source checkout only; auth/download may use Google, document processing is offline.
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
fail() {
    printf '%s\n' '{"command":"drive-docs","exit_code":2,"sync":"not_completed","workflow":"not_run","error":"Source environment missing or stale; run bash setup.sh."}'
    printf 'drive-docs: source environment missing or stale; run bash setup.sh\n' >&2
    exit 2
}
export PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1
unset PYTHONPATH PYTHONSTARTUP
[[ -x "$ROOT/.venv/bin/python" ]] || fail
source "$ROOT/LLM-docs/distribution/python.sh"
select_local_docs_python source || fail
ENV_PY=$("$LOCAL_DOCS_SELECTED" -I "$ROOT/LLM-docs/scripts/git_setup.py" "$ROOT" --check 2>/dev/null) || fail
export DOCNORM_PYTHON="$ENV_PY" DOCSEARCH_PYTHON="$ENV_PY"
exec "$ENV_PY" "$ROOT/LLM-docs/scripts/drive_docs.py" "$@"
