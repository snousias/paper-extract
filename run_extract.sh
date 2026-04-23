#!/usr/bin/env bash
# Optional wrapper: same as: python extract_publications.py  (from this directory)
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python "$HERE/extract_publications.py" "$@"
