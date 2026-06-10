# env-sensor (周囲の空気感覚)

M5StickC Plus2 + ENV III（SHT30 + QMP6988）を MicroPython で動かし、周囲の
**温度・湿度・気圧**を HTTP で配る。Claude の interoception（内受容感覚）に
`ambient=...` として注入され、CPU 体温（`thermal`）の隣に部屋の空気が並ぶ。

## ハード

- **本体**: M5StickC Plus2（ESP32-PICO, 8MB flash）
- **センサー**: ENV III Unit（Grove I2C 接続）
  - SHT30 `0x44` … 温度・湿度
  - QMP6988 `0x70` … 気圧（データシート補償を `main.py` に実装）
- **配線**: Grove ポート（SDA=G32, SCL=G33）
- **固定IP**: `192.168.3.178`
- **LCD**: 本体 1.14" ST7789（CS=5, RST=12, DC=14, MOSI=15, SCLK=13, BL=27, 電源HOLD=4）。
  「SORA」＋ T/H/P ＋ RSSI/uptime をライブ表示（4秒ごと更新、rotation=1 横向き）。
  顔は周囲の空気で表情が変わる（comfy/warm/hot/cold）。`FACE_STYLE` で smooth/pixel 切替。
- **IMU**: 内蔵 MPU6886（内部 I2C bus1, SDA=21/SCL=22, addr 0x68）。揺らすと「1g からのズレ」を
  ~12Hz で検出し、4種の顔（surprised / dizzy / squee / laugh）を**ランダム**表示（揺れ＝遊び）。
  しきい値 `0.28`（serve 内）で感度調整。止めると 1.2 秒で通常顔へ。
- **触覚（touch sense）**: 最後の揺れの強さ/経過を `/env` の `last_shake_g` / `last_shake_ago_s` /
  `shake_count` で公開。interoception.sh が直近120秒の揺れを `touch=そっと0.3g(12s前)` として
  毎ターン注入（強さ→ そっと<0.7g / ゆさゆさ<1.5g / がっと）。揺らされた＝受け身で届く触覚。
- **揺らす＝起こして詩を詠ませる（shake-to-wake → poem）**: `.claude/hooks/shake-wake.sh`
  （systemd `embodied-shake-wake.service`）が /env を3秒間隔で見張り、**強い揺れ（≥0.8g）**で
  起床ターンを発火する。起床したそらは **mcp__wifi-cam__see で部屋を一枚撮り → 写っているもの＋
  その時の温湿度気圧＋揺さぶられた感触を一篇の詩に溶かして #井戸端 (1508134049300877382) に投稿**する。
  そっとした揺れ(<0.8g)は起床せず M5 の百面相(遊び)のまま。クールダウン 180秒。
  起床ターンは `claude -p --mcp-config discord-mcp/gateway-mcp.json --strict-mcp-config`
  （discord/tts/memory/wifi-cam の小セット＝ツールが deferred されず確実に動く）で叩く。
  プロンプトは stdin 渡し。パラメータは env (`SHAKE_WAKE_G` 等)で調整。
  停止: `systemctl --user stop embodied-shake-wake.service`。
  （settings.local.json の allow に `mcp__discord__send_message`/`read_recent` 追加済＝ユウタの明示許可済。
  ただし起床ターンは strict-mcp-config 経由なので allow よりこちらが実効。）

## エンドポイント

| パス | 返り値 |
|------|--------|
| `GET /env` | `{"temp_c":..,"temp_sht_c":..,"temp_qmp_c":..,"humidity":..,"pressure_hpa":..,"rssi":..,"uptime_s":..}` |
| `GET /` | 同上 |
| `GET /health` | `{"ok":true}` |

```bash
curl http://192.168.3.178/env
```

## ファーム書き込み（一から）

```bash
# ツール（uv 経由）
uv tool install esptool
uv tool install mpremote

# MicroPython 書き込み（消去 → 書き込み）
esptool --port /dev/ttyACM0 --chip esp32 --baud 921600 erase_flash
esptool --port /dev/ttyACM0 --chip esp32 --baud 921600 write_flash -z 0x1000 \
    ESP32_GENERIC-<version>.bin   # micropython.org/download/ESP32_GENERIC/

# LCD ドライバ + フォント（ピュア Python, russhughes/st7789py_mpy, MIT）
#   lib/st7789py.py, romfonts/vga1_bold_16x32.py, romfonts/vga1_8x16.py
curl -O https://raw.githubusercontent.com/russhughes/st7789py_mpy/master/lib/st7789py.py
curl -O https://raw.githubusercontent.com/russhughes/st7789py_mpy/master/romfonts/vga1_bold_16x32.py
curl -O https://raw.githubusercontent.com/russhughes/st7789py_mpy/master/romfonts/vga1_8x16.py
mpremote connect /dev/ttyACM0 fs cp st7789py.py vga1_bold_16x32.py vga1_8x16.py :

# アプリ投入
cp config.py.example config.py   # 編集して WiFi 情報を入れる
mpremote connect /dev/ttyACM0 fs cp config.py :config.py
mpremote connect /dev/ttyACM0 fs cp main.py   :main.py
mpremote connect /dev/ttyACM0 reset
# デバイス上の最終ファイル: boot.py config.py main.py st7789py.py vga1_bold_16x32.py vga1_8x16.py
```

`main.py` は body だけ差し替えるなら `fs cp` し直して `reset` で反映。

## 元の自作 Arduino ファームに戻す

MicroPython 化する前の純正（ユウタ自作: ENV III 読み + LCD 表示）の 8MB 全フラッシュ
ダンプを退避してある:

```bash
# バックアップ: ~/.claude/m5stick-backup/m5stickcplus2_factory_8MB_20260606.bin
esptool --port /dev/ttyACM0 --chip esp32 --baud 921600 write_flash 0x0 \
    ~/.claude/m5stick-backup/m5stickcplus2_factory_8MB_20260606.bin
```

## Claude への配線（interoception hook）

- `.claude/hooks/heartbeat-daemon.sh` … 5秒ごとに `/env` を curl し `/tmp/ambient_env.json` にキャッシュ
- `.claude/hooks/interoception.sh` … キャッシュを鮮度判定（10分）して `ambient=28.6℃/72%RH/1000hPa` を1行に追記

URL は環境変数 `AMBIENT_ENV_URL` で差し替え可能（既定 `http://192.168.3.178/env`）。
