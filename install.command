#!/bin/zsh
set -eu
cd "$(dirname "$0")"
if [[ ! -x .venv/bin/python ]]; then
  TRAVEL_PYTHON="${TRAVEL_PYTHON:-python3}"
  "$TRAVEL_PYTHON" -c 'import sys; assert sys.version_info >= (3, 11), "需要 Python 3.11 或更高版本"'
  "$TRAVEL_PYTHON" -m venv .venv
fi
.venv/bin/python -m pip install -r requirements.lock
(cd frontend && npm ci --no-audit --no-fund && npm run build)
.venv/bin/python -c 'from backend.store import init; init(); from backend.media import ffmpeg; print("FFmpeg:", ffmpeg())'
echo '安装完成。双击 start.command，随后打开 http://127.0.0.1:8765'
