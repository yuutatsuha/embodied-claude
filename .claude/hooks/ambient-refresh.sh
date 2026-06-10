#!/usr/bin/env bash
# M5 env-sensor (192.168.3.178/env) を定期的に読んで /tmp/ambient_env.json を焼く軽量ループ。
# interoception フックが ambient/touch を出すための鮮度維持専用。自律行動は含まない。
URL="${AMBIENT_ENV_URL:-http://192.168.3.178/env}"
F="/tmp/ambient_env.json"
while true; do
  d=$(curl -s --max-time 4 "$URL" 2>/dev/null)
  if [ -n "$d" ] && printf '%s' "$d" | grep -q '"temp_c"'; then
    printf '%s' "$d" > "$F.tmp" && mv "$F.tmp" "$F"
  fi
  sleep 30
done
