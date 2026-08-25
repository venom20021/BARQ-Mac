# BARQ-Mac

Mac-native build of [B.A.R.Q-AI](https://github.com/venom20021/B.A.R.Q-AI) — the voice-first AI desktop assistant, tuned for Apple Silicon macOS.

The original **B.A.R.Q-AI** repo remains the cross-platform (Windows-focused) upstream. This fork carries macOS-specific changes only.

## Differences from upstream

| Change | Why |
|---|---|
| `python-bridge.ts` prefers `python/.venv/bin/python` on macOS dev | System `python3` lacks playwright/vosk/torch — sidecar crashed on boot |
| `scripts/com.barq.mac.sidecar.plist` | launchd service: sidecar auto-starts at login, restarts on crash, logs to `/tmp/barq-mac.log` |
| `scripts/start-mac.sh` | One-command app launch; expects sidecar already running via launchd |
| `python/.env` (not committed) | `GEMINI_API_KEY`, `WAKE_WORD=computer` — voice via Gemini Live |
| Port 8956 everywhere | Matches the sidecar default |

## Setup (fresh Mac)

```bash
# 1. Node + Python deps
npm install
cd python && python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium
cd ..

# 2. Environment — create python/.env with:
#    GEMINI_API_KEY=***        (https://aistudio.google.com/apikey)
#    WAKE_WORD=computer
#    BARQ_SKIP_TELEGRAM=false  (once Telegram bot is configured)

# 3. Install the launchd service (auto-start sidecar at login)
cp scripts/com.barq.mac.sidecar.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.barq.mac.sidecar.plist

# 4. Run the desktop app
./scripts/start-mac.sh
```

## Backend

- **Voice** (mic, wake word, TTS): local sidecar on `127.0.0.1:8956`
- **Heavy API** (chat LLM, jobs, knowledge graphs): Oracle Cloud VM (`SIDECAR_REMOTE_URL`, default `http://155.248.247.224`)
- Telegram: morning briefings + job alerts (see `.env`: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`)

## Health checks

```bash
curl http://127.0.0.1:8956/health      # local sidecar
curl http://155.248.247.224/health     # oracle backend
tail -f /tmp/barq-mac.log              # sidecar logs
```

## License

MIT — same as upstream.
