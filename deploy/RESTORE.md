# embodied-claude 配線（デーモン）復元手順

OS 再インストール後、コードを clone/展開して各 MCP の `uv sync` を済ませた後、
常駐デーモンと自律 Heartbeat をこの手順で戻す。秘密（`.env`×3 /
`env-sensor-mcp/firmware/config.py` / `schedule.conf` / `autonomous-action.sh`）は
git に入っていないので、別管理のバックアップから先に戻すこと。

## 0. 前提（このリポに無い秘密・魂を先に戻す）
- `.env`×3（wifi-cam / tts / discord）, `.mcp.json`, `schedule.conf`,
  `autonomous-action.sh`, `env-sensor-mcp/firmware/config.py`(WiFi),
  `go2rtc.yaml` … 暗号化スナップショット等から復元。
- 魂（`~/.claude/memories/memory.db`, `~/.claude/sociality/social.db`,
  `~/.claude/body/`, 自伝MD）… 同上。

## 1. VOICEVOX ENGINE（声の合成エンジン :50021）
外部 ~1.7GB。git にもバックアップにも含めない。GitHub releases から取り直す。
```bash
curl -fL -o /tmp/vv.7z.001 \
  https://github.com/VOICEVOX/voicevox_engine/releases/download/0.25.2/voicevox_engine-linux-cpu-x64-0.25.2.7z.001
cp /tmp/vv.7z.001 /tmp/vv.7z && uvx --from py7zr py7zr x /tmp/vv.7z ~/.local/share/
mv ~/.local/share/linux-cpu-x64 ~/.local/share/voicevox_engine && chmod +x ~/.local/share/voicevox_engine/run
```
> 単一ボリュームの `.7z.001` は `.7z` にリネームで py7zr がそのまま展開できる。

## 2. ⚠️ wifi-cam の venv は Python 3.13 で作る（耳/whisper）
Ubuntu 26.04 の既定 Python は 3.14 で、torch/openai-whisper の wheel がまだ無い。
3.14 のままだと `import whisper` が ModuleNotFoundError。3.13 で作り直す:
```bash
cd wifi-cam-mcp && rm -rf .venv && uv venv --python 3.13 && uv sync --extra dev --extra transcribe
```
（`openai-whisper` は optional extra `transcribe`。素の `uv sync` では入らない。）

## 3. systemd --user デーモン（再起動・ログアウト後も自動起動）
```bash
sudo loginctl enable-linger "$USER"          # ← sudo はこの1回だけ
mkdir -p ~/.config/systemd/user
cp deploy/systemd/*.service ~/.config/systemd/user/
systemctl --user daemon-reload
for s in voicevox-engine ambient-refresh hr-receiver discord-gateway motion-watch; do
  systemctl --user enable --now "$s.service"
done
systemctl --user list-units 'voicevox-engine.service' 'ambient-refresh.service' \
  'hr-receiver.service' 'discord-gateway.service' 'motion-watch.service'
```
- go2rtc は tts-mcp が初回 `say` で自動 DL・起動するので systemd 対象外。
- MCP サーバ（memory/tts/discord/sociality/wifi-cam）は Claude Code が `.mcp.json`
  から起動。手動・systemd 不要。

## 4. 自律 Heartbeat（cron, 20分ごと）
```bash
crontab deploy/crontab.txt
crontab -l
```

## 5. 動作確認
```bash
curl -s http://127.0.0.1:50021/version          # voicevox
ss -tlnp | grep -E ":8787|:1984"                # hr-receiver / go2rtc
systemctl --user --failed                       # 失敗ユニットが無いこと
journalctl --user -u discord-gateway -n 5       # "connected as そら#..." が出るか
```

## ハード再ペアリング（OS入れ替えで切れる）
- Tapo C220 `192.168.3.103`：Tapoアプリで「サードパーティ連携」ON（声）＋マイク ON（耳）。tilt 反転設置。
- M5 env-sensor `192.168.3.178`：firmware/config.py(WiFi) を焼く。`GET /env` 確認。
- Apple Watch 心拍：Health Auto Export を `http://<このPCのIP>:8787` へ（PCのIP変わったら直す）。
