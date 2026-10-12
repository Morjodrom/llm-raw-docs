#!/usr/bin/env bash
# Installation-time provisioning is local only; the workflow remains offline.
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
if [[ ${1:-} == --help || ${1:-} == help || ${1:-} == -h ]]; then
    bash "$ROOT/LLM-docs/local-docs.sh" --help
    exit 0
fi
source "$ROOT/LLM-docs/distribution/python.sh"
if [[ -d "$ROOT/.venv" || -L "$ROOT/.venv" ]]; then
    select_local_docs_python source || exit 2
    INSTALLER="$ROOT/LLM-docs/scripts/git_setup.py"
    INSTALL_ARG=--check
else
    INSTALLER="$ROOT/LLM-docs/distribution/provision.py"
    INSTALL_ARG=
    if [[ ! -f "$ROOT/LLM-docs/distribution/wheelhouse.json" ]]; then
        printf 'local-docs: run bash setup.sh once in a Git checkout (requires internet); document commands never install online\n' >&2
        exit 2
    fi
    select_local_docs_python || exit 2
fi
export PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1
unset PYTHONPATH PYTHONSTARTUP
ENV_PY=$("$LOCAL_DOCS_SELECTED" -I "$INSTALLER" "$ROOT" ${INSTALL_ARG:+"$INSTALL_ARG"}) || exit 2
export DOCNORM_PYTHON="$ENV_PY" DOCSEARCH_PYTHON="$ENV_PY"
exec bash "$ROOT/LLM-docs/local-docs.sh" "$@"
