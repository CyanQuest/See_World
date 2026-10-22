#!/bin/zsh
set -eu
cd "$(dirname "$0")"
if [[ ! -x .venv/bin/python ]]; then
  echo '请先双击 install.command 安装依赖。'
  exit 1
fi
exec .venv/bin/python scripts/run.py
