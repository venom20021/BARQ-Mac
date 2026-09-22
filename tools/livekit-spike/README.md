# LiveKit Agents spike — Phase 1

Scripted-audio latency harness for evaluating `livekit/agents` against BARQ's
current voice stack. **Nothing here is wired into BARQ** — it is a separate
venv and a separate process, so it cannot affect the sidecar's dependencies.

## Why a spike at all

The open question was whether to adopt LiveKit for lower voice latency. It is
only worth it if it measurably beats the current stack, so this exists to
produce a number rather than an opinion.

The key enabler: `livekit-plugins-google` accepts
`gemini-2.5-flash-native-audio-preview-12-2025` — **the exact model BARQ already
runs**. Holding the model constant means any difference measured here is
attributable to the framework/transport, not to the model.

## Setup

Separate venv, Python **3.11** to match BARQ's interpreter (so the interpreter is
not a variable either):

```bash
cd ~/Project/barq-livekit-spike
uv venv --python 3.11
uv pip install livekit-agents livekit-plugins-google livekit-plugins-silero
```

Note: `livekit-agents` requires Python `<3.15`, so the default `python3` (3.14)
will not work. `silero` pulls ONNX via `livekit-local-inference`, not torch.

## Running

```bash
# a scripted utterance, no microphone involved
say -o /tmp/barq_utt.wav --data-format=LEI16@16000 "What is the weather like in Lucknow today"
.venv/bin/python measure.py /tmp/barq_utt.wav 3
```

The harness pushes the WAV into `AgentSession`'s own input stream. There is no
mic, no SFU and no server: `AudioInput` (livekit/agents/voice/io.py) is fed by an
`aio.Chan`, and we assign it ourselves exactly as `RoomIO` does
(`session.input.audio = audio_input`, room_io.py:197). The class mirrors
`ConsoleAudioInput` in `cli/_legacy.py`.

Frames are pushed at 20 ms intervals in **real time** — faster-than-real-time
audio confuses end-of-utterance detection.

## Measurements

Two numbers, chosen to be comparable with BARQ's `GET /voice/latency`:

| metric | definition | BARQ equivalent |
|---|---|---|
| `connect` | `session.start()` until the session is live | `voice_connected` |
| `response` | last input audio frame → first agent "speaking" | `voice_response_ms` |

Observed (n=4, same model, 2.27 s scripted utterance):

```
connect   median ~27–58 ms
response  median ~4257 ms   (min 3758, max 4876)
```

## Two caveats before anyone reads a verdict into this

1. **`connect` is not comparable yet.** `session.start()` returns in tens of ms
   because LiveKit opens the model session lazily; BARQ's `voice_connected`
   (1379 ms measured) *does* include the Gemini Live websocket handshake. These
   are not the same work and must not be compared as if they were.
2. **The comparison is incomplete — BARQ's `response` has no samples.** BARQ's
   `voice_response_ms` only records when the user actually speaks, and the
   `simulate-wake` trigger carries no user audio. To make this a real A/B, BARQ
   needs scripted audio injected into its mic path too, symmetrically.

Until (2) is done there is no verdict, and the honest statement is only that
LiveKit's response latency is **in the same order of magnitude** as BARQ's
current per-turn behaviour, not obviously better.

The measured `response` also includes LiveKit's own VAD endpointing delay
(silero + `min_endpointing_delay`), which is part of what is being evaluated.
