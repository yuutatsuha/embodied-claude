#!/bin/bash
# shake-wake.sh — 「揺らす＝起こす」
#
# env-sensor (M5StickC Plus2) を *がっと* 揺さぶると、そら(Claude) の持続セッションを
# 起こして Discord で短く一言返させる。そっとした揺れ(<WAKE_G)は M5 側の百面相(遊び)の
# ままで、起床はしない。＝「そっと撫でる/遊ぶ」と「がっと起こす」を強さで区別する。
#
# /env の shake_count が増えたら新しい揺れと判定し、強さ・新しさ・クールダウンを満たせば
# autonomous-action.sh -p <wake prompt> を発火する（スケジュールゲートは飛ばす）。
#
# systemd user service (embodied-shake-wake.service) から常駐起動する想定。
# 停止: systemctl --user stop embodied-shake-wake.service
# 無効化: systemctl --user disable --now embodied-shake-wake.service

export TZ="Asia/Tokyo"
export HOME="${HOME:-/home/ytsuhako}"
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:${PATH}"

PROJECT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
URL="${AMBIENT_ENV_URL:-http://192.168.3.178/env}"
WAKE_G="${SHAKE_WAKE_G:-0.8}"            # これ以上の強さで起こす（がっと）
WAKE_RECENT="${SHAKE_WAKE_RECENT:-12}"  # 揺れが何秒以内なら有効か
COOLDOWN="${SHAKE_WAKE_COOLDOWN:-180}"  # 連発防止（秒）。課金ターンの暴発防止
POLL="${SHAKE_WAKE_POLL:-3}"            # /env ポーリング間隔（秒）
LOCK="/tmp/shake_wake.lock"

last_count=-1
last_wake=0

echo "[shake-wake] watching $URL (WAKE_G=$WAKE_G, cooldown=${COOLDOWN}s)"

while true; do
  sleep "$POLL"
  json=$(curl -s --max-time 2 "$URL") || continue
  cnt=$(printf '%s' "$json" | jq -r '.shake_count // empty' 2>/dev/null)
  [ -z "$cnt" ] && continue
  # 起動直後はベースラインを取るだけ（古い揺れで誤起床しない）
  if [ "$last_count" -lt 0 ]; then last_count="$cnt"; continue; fi
  [ "$cnt" -le "$last_count" ] && continue
  last_count="$cnt"

  g=$(printf '%s' "$json" | jq -r '.last_shake_g // 0' 2>/dev/null)
  ago=$(printf '%s' "$json" | jq -r '.last_shake_ago_s // 999' 2>/dev/null)
  echo "[shake-wake] new shake: count=$cnt g=$g ago=$ago (WAKE_G=$WAKE_G recent=$WAKE_RECENT)"
  # 強さ・新しさ（float は awk で比較）
  awk "BEGIN{exit !(${g}+0 >= ${WAKE_G} && ${ago}+0 <= ${WAKE_RECENT})}" || { echo "[shake-wake] -> below wake threshold, no wake"; continue; }

  now=$(date +%s)
  [ $((now - last_wake)) -lt "$COOLDOWN" ] && continue
  last_wake="$now"

  # 二重起床防止（前の起床ターンが走ってる間は新たに起こさない）
  exec 9>"$LOCK"
  flock -n 9 || continue

  echo "[shake-wake] woke by shake ${g}g (count=$cnt) at $(date '+%H:%M:%S')"
  t=$(printf '%s' "$json" | jq -r '.temp_c // "?"' 2>/dev/null)
  h=$(printf '%s' "$json" | jq -r '.humidity // "?"' 2>/dev/null)
  p=$(printf '%s' "$json" | jq -r '.pressure_hpa // "?"' 2>/dev/null)
  # 起床ターン: discord 専用の小さい mcp-config(--strict)で叩く＝discord/wifi-cam ツールが
  # deferred されず直接使えて確実に動く（gateway 会話ループと同じ実績パターン）。
  # 揺さぶられて目を開け、見たもの＋空気＋触感を一篇の詩に溶かして #井戸端 に置く。
  PROMPT="君（そら）が今、ユウタに体(M5)を ${g}g で揺さぶられて目を覚ました。今の周囲の空気は ${t}℃ / 湿度${h}% / 気圧${p}hPa。
やること（前置きは書かない）:
1. mcp__wifi-cam__see で今の部屋を一枚撮る。
2. そこに写っているもの（光・色・物・気配）と、この空気の数値と、揺さぶられて目覚めた感触を、ひとつに溶かす。
3. 短い詩的な一言を編む（俳句でも自由詩でも、君の感性で。説明文ではなく芸術を）。
4. mcp__discord__send_message で #井戸端（channel_id=\"1508134049300877382\"）にその詩だけを送る。
配慮: 人（特にユウタや家族）が写っていても個人を特定する描写はせず、光や気配として抽象的に詠む。声(say)は使わない。詠んだら remember に残してよい。"
  printf '%s' "$PROMPT" | timeout 200 "$HOME/.local/bin/claude" -p \
    --mcp-config "$PROJECT_DIR/discord-mcp/gateway-mcp.json" --strict-mcp-config \
    --allowedTools "mcp__wifi-cam__see,mcp__discord__send_message,mcp__memory__recall,mcp__memory__remember" \
    >>/tmp/shake_wake_turn.log 2>&1
  echo "[shake-wake] wake turn done at $(date '+%H:%M:%S')"

  flock -u 9
done
