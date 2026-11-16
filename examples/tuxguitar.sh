#!/usr/bin/env bash
#
# Example: generate a UML collection for the TuxGuitar checkout.
#
# Adjust the paths for your machine.
#
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO="${TUXGUITAR_REPO:-$HOME/data/analysis/git/tuxguitar-patterns}"
OUT="${TUXGUITAR_UML:-$HOME/uml/tuxguitar}"

exec bash "$PROJECT_DIR/run.sh" generate \
    --collection "$OUT" \
    --src "$REPO" \
    --exclude 'gervill/**' \
    --exclude 'TuxGuitar-android-gervill/**' \
    --exclude '**/target/**' \
    --group-by module-package \
    --split-threshold 0 \
    --relations full \
    --format both \
    --index \
    --coverage
