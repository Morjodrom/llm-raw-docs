#!/usr/bin/env bash
# Installation-time provisioning is local only; the workflow remains offline.
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
if [[ ${1:-} == --help || ${1:-} == help || ${1:-} == -h ]]; then
    bash "$ROOT/LLM-docs/local-docs.sh" --help
    exit 0
fi
source "$ROOT/LLM-docs/distribution/python.sh"
select_local_docs_python || exit 2
export PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1
unset PYTHONPATH PYTHONSTARTUP
ENV_PY=$("$LOCAL_DOCS_SELECTED" -I "$ROOT/LLM-docs/distribution/provision.py" "$ROOT") || exit 2
export DOCNORM_PYTHON="$ENV_PY" DOCSEARCH_PYTHON="$ENV_PY"
exec bash "$ROOT/LLM-docs/local-docs.sh" "$@"
