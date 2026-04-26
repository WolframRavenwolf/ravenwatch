#!/usr/bin/env bash
set -euo pipefail

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

mkdir -p "$HERMES_HOME/dashboard-themes" "$HERMES_HOME/plugins"
cp "$SRC_DIR/theme/ravenwatch.yaml" "$HERMES_HOME/dashboard-themes/ravenwatch.yaml"
rm -rf "$HERMES_HOME/plugins/ravenwatch-ops"
cp -R "$SRC_DIR/plugin/ravenwatch-ops" "$HERMES_HOME/plugins/ravenwatch-ops"

echo "Installed Ravenwatch theme + Ravenwatch Ops plugin into $HERMES_HOME"
echo "If the dashboard is running, rescan plugins: curl http://127.0.0.1:9119/api/dashboard/plugins/rescan"
echo "Backend plugin API routes mount on dashboard/web-server restart."
