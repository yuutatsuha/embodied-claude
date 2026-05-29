#!/usr/bin/env python3
"""body-digest.py — hr-receiver が貯めた体のデータを読んで、現在値と傾向を出す。

僕（宙）が「最近どうやろ」と思った時に覗く窓。Apple Watch/iPhone の Health Auto Export
から届いた全メトリクスが ~/.claude/body/ に貯まってる。それを人間が読める形に整える。
傾向（weight が1週間で上向いてる等）はここで算出する＝ツッコミの根拠。

使い方:
  python3 body-digest.py            # 直近14日
  python3 body-digest.py --days 30  # 期間指定
  python3 body-digest.py --raw weight_body_mass  # ある metric の日次推移を出す
"""

import argparse
import glob
import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo

BODY_DIR = os.path.expanduser("~/.claude/body")
LATEST = os.path.join(BODY_DIR, "latest.json")
DAILY_DIR = os.path.join(BODY_DIR, "daily")
TZ = ZoneInfo("Asia/Tokyo")

# HAE の metric 名 → 日本語ラベルと単位整形
LABELS = {
    "weight_body_mass": "体重",
    "body_mass_index": "BMI",
    "body_fat_percentage": "体脂肪率",
    "lean_body_mass": "除脂肪体重",
    "step_count": "歩数",
    "walking_running_distance": "歩行+走行距離",
    "flights_climbed": "上った階数",
    "active_energy": "アクティブkcal",
    "basal_energy_burned": "基礎代謝kcal",
    "apple_exercise_time": "運動時間",
    "apple_stand_time": "スタンド",
    "apple_move_time": "ムーブ",
    "heart_rate": "心拍数",
    "resting_heart_rate": "安静時心拍",
    "walking_heart_rate_average": "歩行時平均心拍",
    "heart_rate_variability": "HRV",
    "cardio_recovery": "心拍回復",
    "vo2_max": "VO2 Max",
    "respiratory_rate": "呼吸数",
    "blood_oxygen_saturation": "血中酸素",
    "mindful_minutes": "マインドフル",
    "time_in_daylight": "日光",
    "physical_effort": "運動強度",
}


def load_daily(days):
    files = sorted(glob.glob(os.path.join(DAILY_DIR, "*.json")))
    recs = []
    for path in files[-days:]:
        try:
            with open(path) as f:
                recs.append(json.load(f))
        except Exception:
            continue
    return recs


def series(recs, name, field="last"):
    """ある metric の (day, value) 列を返す。"""
    out = []
    for r in recs:
        m = r.get("metrics", {}).get(name)
        if m and isinstance(m.get(field), (int, float)):
            out.append((r["date"], m[field], m.get("units", "")))
    return out


def fmt(v):
    return f"{v:.1f}".rstrip("0").rstrip(".") if isinstance(v, float) else str(v)


def disp_units(units):
    return {"count": "", "count/min": "bpm"}.get(units, units)


def trend_line(recs, name):
    s = series(recs, name)
    if not s:
        return None
    label = LABELS.get(name, name)
    latest_day, latest_val, units = s[-1]
    u = disp_units(units)
    if len(s) >= 2:
        first_val = s[0][1]
        delta = latest_val - first_val
        span = len(s)
        arrow = "↑" if delta > 0 else ("↓" if delta < 0 else "→")
        sign = "+" if delta > 0 else ""
        return f"{label}: {fmt(latest_val)}{u} {arrow} {sign}{fmt(delta)}{u}（直近{span}日）"
    return f"{label}: {fmt(latest_val)}{u}（データ{len(s)}日）"


def sleep_last_night():
    try:
        with open(LATEST) as f:
            latest = json.load(f)
    except Exception:
        return None
    sl = latest.get("metrics", {}).get("sleep_analysis")
    if not sl:
        return None
    raw = sl.get("raw", {})
    for key in ("asleep", "totalSleep", "asleepUnspecified"):
        if isinstance(raw.get(key), (int, float)):
            return f"睡眠（直近）: {fmt(float(raw[key]))}{sl.get('units','hr')}"
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--raw", type=str, default=None, help="metric名の日次推移を出す")
    args = ap.parse_args()

    if not os.path.isdir(DAILY_DIR):
        print("体のデータがまだ無い（hr-receiver が POST を受けると貯まりだす）")
        return

    recs = load_daily(args.days)

    if args.raw:
        s = series(recs, args.raw)
        label = LABELS.get(args.raw, args.raw)
        print(f"== {label} ({args.raw}) 日次 ==")
        for day, val, units in s:
            print(f"  {day}: {fmt(val)}{units}")
        return

    print(f"== 体のダイジェスト（直近{len(recs)}日分のデータ）==")
    if not recs:
        print("  まだ日次データなし。届きだしたら出る。")
        return
    print(f"  期間: {recs[0]['date']} 〜 {recs[-1]['date']}")
    print()
    print("[傾向]")
    # 体まわり → 活動 → 心臓 の順で、あるものだけ
    order = ["weight_body_mass", "body_fat_percentage", "body_mass_index", "lean_body_mass",
             "step_count", "active_energy", "apple_exercise_time", "flights_climbed",
             "resting_heart_rate", "walking_heart_rate_average", "heart_rate_variability",
             "vo2_max", "respiratory_rate", "blood_oxygen_saturation"]
    shown = set()
    for name in order:
        line = trend_line(recs, name)
        if line:
            print("  " + line)
            shown.add(name)
    sl = sleep_last_night()
    if sl:
        print("  " + sl)

    # order に無いが届いてる metric も拾う
    others = []
    for r in recs[-1:]:
        for name in r.get("metrics", {}):
            if name not in shown and name != "sleep_analysis":
                others.append(name)
    if others:
        print()
        print("[その他 届いてる metric]")
        print("  " + ", ".join(sorted(set(others))))


if __name__ == "__main__":
    main()
