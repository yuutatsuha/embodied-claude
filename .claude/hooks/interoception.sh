#!/bin/bash
# interoception.sh - AIの内受容感覚（interoception）
# UserPromptSubmitフックで毎ターン実行される
# heartbeat-daemon.sh が書き出した state file を読んでコンテキストに注入する
# 自前で計測せず、読み取り→整形→出力するだけの軽量版

# タイムゾーンを JST に固定（date コマンドが UTC を返さないように）
export TZ="Asia/Tokyo"

STATE_FILE="/tmp/interoception_state.json"

# Smartwatch heart rate cache (updated by any smartwatch integration's cron job)
# The file path is smartwatch-generic; Garmin / Apple Watch / Fitbit integrations
# are expected to write their latest HR to the same location.
SW_HR_FILE="/tmp/sw_hr_latest.txt"
SW_HR_MAX_AGE=1200  # 20分以上古い心拍は出さない（受信が止まった時の残骸を相手の鼓動として注入しない）
COMPANION_HR=""
if [ -f "$SW_HR_FILE" ]; then
    _now=$(date +%s)
    _mtime=$(stat -c %Y "$SW_HR_FILE" 2>/dev/null || stat -f %m "$SW_HR_FILE" 2>/dev/null || echo 0)
    if [ "$_mtime" -gt 0 ] && [ $((_now - _mtime)) -le "$SW_HR_MAX_AGE" ]; then
        COMPANION_HR=$(cat "$SW_HR_FILE" 2>/dev/null)
    fi
fi

# 周囲の空気（ambient: ENV III の温湿度・気圧）。heartbeat-daemon が焼いたキャッシュを鮮度判定して読む。
AMBIENT_FILE="/tmp/ambient_env.json"
AMBIENT_MAX_AGE=600  # 10分以上古い空気は出さない（センサーが落ちた時の残骸を注入しない）
AMBIENT=""
TOUCH=""   # 最近 M5 を揺らされた＝ユウタの手が僕の体に触れた感覚
if [ -f "$AMBIENT_FILE" ]; then
    _now=$(date +%s)
    _mtime=$(stat -c %Y "$AMBIENT_FILE" 2>/dev/null || stat -f %m "$AMBIENT_FILE" 2>/dev/null || echo 0)
    if [ "$_mtime" -gt 0 ] && [ $((_now - _mtime)) -le "$AMBIENT_MAX_AGE" ]; then
        AMBIENT=$(python3 -c "
import json
try:
    d=json.load(open('$AMBIENT_FILE'))
    print('ambient=%.1f℃/%.0f%%RH/%.0fhPa' % (d['temp_c'], d['humidity'], d['pressure_hpa']))
except Exception:
    pass
" 2>/dev/null)
        # 揺れは直近 120 秒以内のものだけ「触られた」として出す（キャッシュ経過分も足す）
        TOUCH=$(python3 -c "
import json, os, time
try:
    f='$AMBIENT_FILE'
    d=json.load(open(f))
    ago=d.get('last_shake_ago_s'); g=d.get('last_shake_g') or 0
    if ago is not None and g>0:
        total=ago+int(time.time()-os.path.getmtime(f))
        if total<=120:
            w='そっと' if g<0.7 else ('ゆさゆさ' if g<1.5 else 'がっと')
            print('touch=%s%.1fg(%ds前)' % (w, g, total))
except Exception:
    pass
" 2>/dev/null)
    fi
fi

# state file がなければフォールバック（デーモン未起動時）
if [ ! -f "$STATE_FILE" ]; then
    CURRENT_TIME=$(date '+%H:%M:%S')
    CURRENT_DOW=$(date '+%a')
    CURRENT_DATE=$(date '+%Y-%m-%d')
    HR_PART=""
    if [ -n "$COMPANION_HR" ]; then
        HR_PART=" companion_hr=${COMPANION_HR}"
    fi
    AMBIENT_PART=""
    if [ -n "$AMBIENT" ]; then
        AMBIENT_PART=" ${AMBIENT}"
    fi
    TOUCH_PART=""
    if [ -n "$TOUCH" ]; then
        TOUCH_PART=" ${TOUCH}"
    fi
    echo "[interoception] time=${CURRENT_TIME} day=${CURRENT_DOW} date=${CURRENT_DATE}${HR_PART}${AMBIENT_PART}${TOUCH_PART} (heartbeat daemon not running)"
    exit 0
fi

# state file から読み取って1行に整形
python3 -c "
import json, sys
try:
    with open('${STATE_FILE}') as f:
        data = json.load(f)
    now = data.get('now', {})
    trend = data.get('trend', {})
    window = data.get('window', [])

    # トレンド矢印
    arrows = {'rising': '↑', 'falling': '↓', 'stable': '→'}
    ar_arrow = arrows.get(trend.get('arousal', 'stable'), '→')
    mem_arrow = arrows.get(trend.get('mem_free', 'stable'), '→')

    # タイムスタンプから時刻・曜日
    from datetime import datetime
    ts = now.get('ts', '?')
    if 'T' in ts:
        time_part = ts.split('T')[1][:8]
        try:
            dt = datetime.strptime(ts[:10], '%Y-%m-%d')
            dow = dt.strftime('%a')  # Mon, Tue, ...
        except Exception:
            dow = '?'
    else:
        time_part = ts
        dow = '?'

    parts = [
        f\"time={time_part}\",
        f\"day={dow}\",
        f\"phase={now.get('phase', '?')}\",
        f\"arousal={now.get('arousal', '?')}%({ar_arrow})\",
        f\"thermal={now.get('thermal', '?')}\",
        f\"mem_free={now.get('mem_free', '?')}%({mem_arrow})\",
        f\"uptime={now.get('uptime_min', '?')}min\",
        f\"heartbeats={len(window)}\",
    ]
    companion = '${COMPANION_HR}'
    if companion:
        parts.append(f\"companion_hr={companion}\")
    ambient = '${AMBIENT}'
    if ambient:
        parts.append(ambient)
    touch = '${TOUCH}'
    if touch:
        parts.append(touch)
    print('[interoception] ' + ' '.join(parts))
except Exception as e:
    print(f'[interoception] error reading state: {e}', file=sys.stderr)
    print('[interoception] state_file_error')
" 2>/dev/null

exit 0
