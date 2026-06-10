#!/bin/bash
# motion-watch.sh — motion-watch.py を wifi-cam-mcp の venv(onvif/dotenv 入り)で起動する薄い殻。
# TZ を JST 固定（深夜帯ゲートが UTC でずれないように）。systemd user service から常駐する想定。
export TZ="Asia/Tokyo"
export HOME="${HOME:-/home/ytsuhako}"
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:${PATH}"

PROJECT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
VENV_PY="$PROJECT_DIR/wifi-cam-mcp/.venv/bin/python"
[ -x "$VENV_PY" ] || VENV_PY="python3"

exec "$VENV_PY" "$PROJECT_DIR/.claude/hooks/motion-watch.py"
