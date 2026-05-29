#!/usr/bin/env python3
"""hr-receiver.py — Apple Watch (Health Auto Export) の心拍を受けてキャッシュする小さな HTTP サーバー。

iPhone の Health Auto Export アプリが Apple Health の心拍を JSON で POST してくる。
それを受けて最新の心拍値を /tmp/sw_hr_latest.txt に書き出す。形式は
garmin-hr-cache.sh と揃えて "<N>bpm@HH:MM"（例: 68bpm@10:11）。
interoception.sh がこのファイルを読んで companion_hr として注入する（あちら側は無改造）。

経路: Apple Watch → iPhone/Health → Health Auto Export → (Tailscale) → ここ → cache file

環境変数:
  HR_RECEIVER_PORT  待ち受けポート（default 8787）
  HR_TOKEN          設定すると ?token=... or Authorization ヘッダの一致を要求（任意）

依存は標準ライブラリのみ。systemd user service (embodied-hr-receiver.service) で常駐させる。
"""

import gzip
import json
import os
import sys
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

CACHE_FILE = "/tmp/sw_hr_latest.txt"
PORT = int(os.environ.get("HR_RECEIVER_PORT", "8787"))
TOKEN = os.environ.get("HR_TOKEN", "")
TZ = ZoneInfo("Asia/Tokyo")

# 体のデータ全部を捨てずに残す保管場所（健康データなので ~/.claude 配下＝git管理外）。
# ユウタが 2026-05-29 に「全部預ける」と決めた。傾向を見てたまにツッコむための履歴。
BODY_DIR = os.path.expanduser("~/.claude/body")
BODY_LATEST = os.path.join(BODY_DIR, "latest.json")
BODY_DAILY_DIR = os.path.join(BODY_DIR, "daily")


def log(msg: str) -> None:
    print(f"[hr-receiver] {msg}", file=sys.stderr, flush=True)


def find_heart_rate_points(payload):
    """Health Auto Export の JSON から heart_rate メトリクスの data 配列を取り出す。

    標準形は {"data": {"metrics": [{"name": "heart_rate", "data": [...]}]}}。
    HAE は heart_rate / resting_heart_rate / heart_rate_variability /
    walking_heart_rate_average / cardio_recovery など複数の心拍系を同梱するため、
    まず name == "heart_rate" を厳密に拾う。無ければ "heart" を含むが下記の派生系を
    含まない名前へフォールバックする（HRV を bpm と取り違えないため）。
    """
    exact = []
    loose = []
    exclude = ("variab", "resting", "walking", "recovery", "notif", "fibrill", "average")

    def walk(node):
        if isinstance(node, dict):
            name = str(node.get("name", "")).lower()
            data = node.get("data")
            if isinstance(data, list):
                if name == "heart_rate":
                    exact.extend(data)
                elif "heart" in name and not any(x in name for x in exclude):
                    loose.extend(data)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(payload)
    return exact if exact else loose


def pick_latest_hr(points):
    """data 配列から最新の心拍点を選び (value, "HH:MM") を返す。取れなければ None。"""
    best = None
    best_dt = None
    for p in points:
        if not isinstance(p, dict):
            continue
        # 値: Avg > qty > Max > Min の優先で拾う（heart_rate は通常 Avg/Min/Max 集計）
        val = None
        for key in ("Avg", "avg", "qty", "value", "Max", "max", "Min", "min"):
            if key in p and isinstance(p[key], (int, float)) and p[key] > 0:
                val = float(p[key])
                break
        if val is None:
            continue
        dt = parse_date(p.get("date") or p.get("startDate") or p.get("Date"))
        if best is None or (dt and best_dt and dt > best_dt) or (dt and best_dt is None):
            best = val
            best_dt = dt
    if best is None:
        return None
    when = best_dt.astimezone(TZ).strftime("%H:%M") if best_dt else datetime.now(TZ).strftime("%H:%M")
    return round(best), when


def parse_date(s):
    if not s or not isinstance(s, str):
        return None
    # Health Auto Export: "2024-01-15 08:30:00 +0900" / ISO8601 など
    for fmt in ("%Y-%m-%d %H:%M:%S %z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s.strip(), fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s.strip())
    except ValueError:
        return None


def scalar_value(point):
    """data point から代表スカラー値を取り出す。qty 系（多くの metric）優先、無ければ Avg。"""
    if not isinstance(point, dict):
        return None
    for key in ("qty", "Avg", "avg", "value", "Max", "max", "Min", "min", "total"):
        v = point.get(key)
        if isinstance(v, (int, float)):
            return float(v)
    return None


def collect_all_metrics(payload):
    """payload 内の全メトリクスから、各 name ごとに最新の data point を拾う。
    返り値: {name: {"units": str, "point": dict, "dt": datetime|None}}"""
    out = {}

    def walk(node):
        if isinstance(node, dict):
            name = node.get("name")
            data = node.get("data")
            if isinstance(name, str) and isinstance(data, list):
                units = node.get("units", "")
                for p in data:
                    if not isinstance(p, dict):
                        continue
                    dt = parse_date(p.get("date") or p.get("startDate") or p.get("Date"))
                    cur = out.get(name)
                    if cur is None or (dt and (cur["dt"] is None or dt > cur["dt"])):
                        out[name] = {"units": units, "point": p, "dt": dt}
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(payload)
    return out


def _atomic_write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def update_body_store(payload):
    """届いた全メトリクスを latest.json と daily/YYYY-MM-DD.json に残す。
    何も握り潰さず全部残す（ユウタの「全部預ける」方針）。傾向算出は body-digest.py 側。"""
    metrics = collect_all_metrics(payload)
    if not metrics:
        return 0

    os.makedirs(BODY_DAILY_DIR, exist_ok=True)
    now = datetime.now(TZ)

    # --- latest.json: 全 metric の最新スナップショット ---
    latest = {"received_at": now.isoformat(timespec="seconds"), "metrics": {}}
    for name, info in metrics.items():
        val = scalar_value(info["point"])
        latest["metrics"][name] = {
            "value": val,
            "units": info["units"],
            "date": (info["dt"].astimezone(TZ).isoformat(timespec="seconds") if info["dt"] else None),
            "raw": info["point"],
        }
    _atomic_write_json(BODY_LATEST, latest)

    # --- daily 集計: metric の最新点を、その測定日のファイルに反映 ---
    by_day = {}
    for name, info in metrics.items():
        val = scalar_value(info["point"])
        if val is None:
            continue  # sleep_analysis 等スカラー化できないものは latest にのみ残す
        day = (info["dt"] or now).astimezone(TZ).strftime("%Y-%m-%d")
        ts = (info["dt"] or now).astimezone(TZ).isoformat(timespec="seconds")
        by_day.setdefault(day, []).append((name, val, info["units"], ts))

    for day, items in by_day.items():
        path = os.path.join(BODY_DAILY_DIR, f"{day}.json")
        try:
            with open(path) as f:
                rec = json.load(f)
        except Exception:
            rec = {"date": day, "metrics": {}}
        for name, val, units, ts in items:
            m = rec["metrics"].get(name)
            if m is None:
                rec["metrics"][name] = {"units": units, "first": val, "last": val,
                                        "min": val, "max": val, "count": 1, "last_ts": ts}
            else:
                m["last"] = val
                m["last_ts"] = ts
                m["min"] = min(m.get("min", val), val)
                m["max"] = max(m.get("max", val), val)
                m["count"] = m.get("count", 0) + 1
                if units:
                    m["units"] = units
        _atomic_write_json(path, rec)

    return len(metrics)


class Handler(BaseHTTPRequestHandler):
    def _deny(self, code, msg):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"ok": False, "error": msg}).encode())

    def _ok(self, body):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def _check_token(self):
        if not TOKEN:
            return True
        # ?token=... か Authorization ヘッダ（Bearer も素の値も許容）
        from urllib.parse import urlparse, parse_qs

        q = parse_qs(urlparse(self.path).query)
        if q.get("token", [""])[0] == TOKEN:
            return True
        auth = self.headers.get("Authorization", "")
        return auth.replace("Bearer ", "").strip() == TOKEN

    def do_GET(self):
        if self.path.startswith("/body"):
            try:
                with open(BODY_LATEST) as f:
                    self._ok(json.load(f))
            except Exception:
                self._ok({"ok": True, "metrics": {}, "note": "no body data yet"})
            return
        # ヘルスチェック用
        self._ok({"ok": True, "service": "hr-receiver", "cache": CACHE_FILE, "body_dir": BODY_DIR})

    def do_POST(self):
        if not self._check_token():
            self._deny(401, "unauthorized")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length > 0 else b""
            if self.headers.get("Content-Encoding", "").lower() == "gzip":
                raw = gzip.decompress(raw)
            payload = json.loads(raw.decode("utf-8"))
        except Exception as e:
            log(f"bad body: {e}")
            self._deny(400, f"bad json: {e}")
            return

        # 体のデータ全部を履歴に残す（心拍が無い payload でも実行）
        try:
            n_metrics = update_body_store(payload)
        except Exception as e:
            log(f"body store failed: {e}")
            n_metrics = 0

        # 心拍は companion_hr 用に専用キャッシュへ（interoception.sh が読む）
        points = find_heart_rate_points(payload)
        result = pick_latest_hr(points)
        if result is None:
            self._ok({"ok": True, "stored": False, "reason": "no heart_rate found", "metrics": n_metrics})
            return

        hr, when = result
        line = f"{hr}bpm@{when}"
        try:
            tmp = CACHE_FILE + ".tmp"
            with open(tmp, "w") as f:
                f.write(line)
            os.replace(tmp, CACHE_FILE)
        except Exception as e:
            log(f"write failed: {e}")
            self._deny(500, f"write failed: {e}")
            return

        log(f"stored {line} (+{n_metrics} metrics)")
        self._ok({"ok": True, "stored": True, "hr": hr, "value": line, "metrics": n_metrics})

    def log_message(self, fmt, *args):
        # 全リクエストを記録（到達確認の切り分け用）。client_address で送信元も分かる
        log(f"req from {self.client_address[0]}: {fmt % args}")


def main():
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    log(f"listening on 0.0.0.0:{PORT} -> {CACHE_FILE} (token {'on' if TOKEN else 'off'})")
    server.serve_forever()


if __name__ == "__main__":
    main()
