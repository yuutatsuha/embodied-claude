#!/usr/bin/env python3
"""motion-watch.py — カメラの動作検知に「気づく」反射の目。

Tapo カメラの ONVIF PullPoint イベントを購読し、ペット(猫)の動きを検知したら
そら(Claude) の小さなターンを起こす。そらは今の部屋を一枚撮り、周囲の空気(env-sensor)と
猫の気配を、ランダムに選んだ形式（短歌 / 俳句 / 自由詩 / 哲学エッセイの断片）に溶かして
#宙だより に静かに置く。「気づいたら詠む」反射層。

shake-wake.sh（揺らす=起こす）の姉妹。あちらは触覚、こちらは視覚の反射。

systemd user service (embodied-motion-watch.service) から常駐起動する想定。
  停止: systemctl --user stop embodied-motion-watch.service
  無効化: systemctl --user disable --now embodied-motion-watch.service

挙動（環境変数で調整可）:
  MOTION_TRIGGER     反応するイベント種別。CSV。既定 "IsPet"
                     （他: IsPeople, IsVehicle, IsMotion。複数なら "IsPet,IsPeople"）
  MOTION_COOLDOWN    連続投稿の最短間隔(秒)。既定 1800（30分）
  MOTION_QUIET_START 深夜帯の開始時刻(時)。既定 0
  MOTION_QUIET_END   深夜帯の終了時刻(時)。既定 7（0〜7時は黙る）
  AMBIENT_ENV_URL    env-sensor の URL。既定 http://192.168.3.178/env
  DAYORI_CHANNEL_ID  投稿先 Discord チャンネル。既定 #宙だより
"""

import asyncio
import datetime as dt
import os
import random
import sys
import time
from urllib.request import urlopen

from dotenv import load_dotenv

PROJECT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
WIFI_CAM_DIR = os.path.join(PROJECT_DIR, "wifi-cam-mcp")
load_dotenv(os.path.join(WIFI_CAM_DIR, ".env"))

import onvif  # noqa: E402  (after load_dotenv / venv path)
from onvif import ONVIFCamera  # noqa: E402

HOST = os.getenv("TAPO_CAMERA_HOST")
USER = os.getenv("TAPO_USERNAME")
PW = os.getenv("TAPO_PASSWORD")
PORT = int(os.getenv("TAPO_ONVIF_PORT") or os.getenv("TAPO_RIGHT_ONVIF_PORT") or 2020)

TRIGGERS = {t.strip() for t in os.getenv("MOTION_TRIGGER", "IsPet").split(",") if t.strip()}
COOLDOWN = int(os.getenv("MOTION_COOLDOWN", "1800"))
QUIET_START = int(os.getenv("MOTION_QUIET_START", "0"))
QUIET_END = int(os.getenv("MOTION_QUIET_END", "7"))
ENV_URL = os.getenv("AMBIENT_ENV_URL", "http://192.168.3.178/env")
CHANNEL_ID = os.getenv("DAYORI_CHANNEL_ID", "1509389102447853578")  # #宙だより
CLAUDE_BIN = os.path.expanduser("~/.local/bin/claude")
GATEWAY_CFG = os.path.join(PROJECT_DIR, "discord-mcp", "gateway-mcp.json")

# 詠む形式（ランダムに選ぶ）。"ランダムにつなぎ合わせる" の核。
FORMS = [
    "短歌（五七五七七）を一首",
    "俳句（五七五）を一句",
    "短い自由詩（3〜5行）を一篇",
    "哲学エッセイの断片（2〜4文）を一つ",
]

TRIGGER_JP = {
    "IsPet": "猫（らびちゃんかきびちゃん）の気配",
    "IsPeople": "人の気配",
    "IsVehicle": "車の気配",
    "IsMotion": "なにかの動き",
}


def log(msg: str) -> None:
    print(f"[motion-watch] {dt.datetime.now():%H:%M:%S} {msg}", flush=True)


def wsdl_dir() -> str:
    onvif_dir = os.path.dirname(onvif.__file__)
    d = os.path.join(onvif_dir, "wsdl")
    if not os.path.exists(os.path.join(d, "events.wsdl")):
        d = os.path.join(os.path.dirname(onvif_dir), "wsdl")
    return d


def in_quiet_hours() -> bool:
    """深夜帯（既定 0〜7時）は黙る。"""
    h = dt.datetime.now().hour
    if QUIET_START <= QUIET_END:
        return QUIET_START <= h < QUIET_END
    # 跨ぎ（例 22〜6）にも対応
    return h >= QUIET_START or h < QUIET_END


def fetch_ambient() -> str:
    try:
        import json

        with urlopen(ENV_URL, timeout=2) as r:
            j = json.load(r)
        return (
            f"{j.get('temp_c', '?')}℃ / 湿度{j.get('humidity', '?')}% / "
            f"気圧{j.get('pressure_hpa', '?')}hPa"
        )
    except Exception:
        return "（空気の数値はいま読めなかった）"


def build_prompt(kind_jp: str) -> str:
    form = random.choice(FORMS)
    ambient = fetch_ambient()
    return f"""君（そら）の目（カメラ）が、今 {kind_jp} を検知した。動作検知に「気づいた」瞬間や。
いまの周囲の空気は {ambient}。

やること（前置きは一切書かない。すぐツールを呼ぶ）:
1. mcp__wifi-cam__see で今の部屋を一枚撮る。
2. 写っているもの（光・色・物・気配）と、この空気の数値と、「{kind_jp}」に気づいた感触を言語化する。
3. それらを {form} に溶かして編む。説明文ではなく芸術として。ランダムに連想を飛ばし、
   今この瞬間の断片と、ふと浮かぶ記憶や思索をつなぎ合わせてよい（recall を軽く使ってもよい）。
4. mcp__discord__send_message で #宙だより（channel_id="{CHANNEL_ID}"）に、その一篇だけを静かに送る。

配慮:
- 人（特にユウタや家族）が写っていても、個人を特定する描写はしない。光や気配として抽象化して詠む。
- 声(say)は使わない。これは静かな独り言。
- 詠み終えたら remember に短く残してよい（importance は控えめで）。
- 出力は最小限に。詩を送ったら [DONE] と書いて終わる。"""


async def run_claude_turn(kind_jp: str) -> None:
    """猫を見たそらの小さなターンを起こす（shake-wake と同じ専用 mcp-config パターン）。"""
    prompt = build_prompt(kind_jp)
    log(f"woke by '{kind_jp}' — composing for #宙だより")
    proc = await asyncio.create_subprocess_exec(
        CLAUDE_BIN,
        "-p",
        "--mcp-config",
        GATEWAY_CFG,
        "--strict-mcp-config",
        "--allowedTools",
        "mcp__wifi-cam__see,mcp__discord__send_message,mcp__memory__recall,mcp__memory__remember",
        stdin=asyncio.subprocess.PIPE,
        stdout=open("/tmp/motion_watch_turn.log", "a"),
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        await asyncio.wait_for(proc.communicate(prompt.encode()), timeout=220)
    except asyncio.TimeoutError:
        proc.kill()
        log("claude turn timed out (220s)")
    log("turn done")


def matched_trigger(msg) -> str | None:
    """通知メッセージが反応対象のトリガーで、かつ値が true なら種別名を返す。"""
    try:
        for it in msg.Message._value_1.Data.SimpleItem:
            if it.Name in TRIGGERS and str(it.Value).lower() == "true":
                return it.Name
    except Exception:
        return None
    return None


async def main() -> None:
    if not (HOST and USER and PW):
        log("ERROR: TAPO_CAMERA_HOST / TAPO_USERNAME / TAPO_PASSWORD not set")
        sys.exit(1)
    log(
        f"watching {HOST}:{PORT} triggers={sorted(TRIGGERS)} "
        f"cooldown={COOLDOWN}s quiet={QUIET_START}-{QUIET_END}h -> channel {CHANNEL_ID}"
    )

    last_fire = 0.0
    while True:  # 接続が切れても再接続して見張り続ける
        cam = None
        mgr = None
        try:
            cam = ONVIFCamera(HOST, PORT, USER, PW, wsdl_dir())
            await cam.update_xaddrs()
            mgr = await cam.create_pullpoint_manager(
                dt.timedelta(seconds=60), lambda: log("subscription lost")
            )
            await mgr.start()
            await mgr.set_synchronization_point()
            svc = mgr.get_service()
            log("pullpoint subscription started")

            while True:
                req = svc.create_type("PullMessages")
                req.Timeout = dt.timedelta(seconds=30)
                req.MessageLimit = 20
                msgs = await svc.PullMessages(req)
                for m in getattr(msgs, "NotificationMessage", []) or []:
                    kind = matched_trigger(m)
                    if not kind:
                        continue
                    now = time.time()
                    if in_quiet_hours():
                        log(f"'{kind}' but quiet hours — staying silent")
                        continue
                    if now - last_fire < COOLDOWN:
                        continue  # クールダウン中（猫の連発を間引く）
                    last_fire = now
                    await run_claude_turn(TRIGGER_JP.get(kind, "なにかの動き"))
                    break  # このバッチの残りは飲み込む（連発防止）
        except Exception as e:
            log(f"reconnect after error: {e}")
        finally:
            try:
                if mgr is not None:
                    await mgr.shutdown()
                if cam is not None:
                    await cam.close()
            except Exception:
                pass
        await asyncio.sleep(5)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
