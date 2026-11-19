#!/usr/bin/env bash
# Bench report for the installation: run with the service up, send the two files it prints.
#   ./bench.sh [--laser-distance METRES] [--camera-test]
set -euo pipefail
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$APP_DIR/.venv/bin/python" "$APP_DIR/tools/bench_report.py" "$@"
