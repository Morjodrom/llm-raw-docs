#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
source "$ROOT/LLM-docs/distribution/python.sh"
select_local_docs_python || exit 2
exec "$LOCAL_DOCS_SELECTED" -I "$ROOT/LLM-docs/distribution/build.py" "$ROOT" "$@"
