#!/usr/bin/env bash
# Explicit provisioning only. Document commands never install from the network.
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
source "$ROOT/LLM-docs/distribution/python.sh"
select_local_docs_python source || exit 2
exec "$LOCAL_DOCS_SELECTED" -I "$ROOT/LLM-docs/scripts/git_setup.py" "$ROOT" "$@"
