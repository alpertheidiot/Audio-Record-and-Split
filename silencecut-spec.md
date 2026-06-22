# SilenceCut — Project Spec

> Working title. A localhost macOS app that captures system audio losslessly, auto-records on signal, auto-stops on silence, trims trailing silence, and transcodes to a chosen lossy/lossless format.

This file is the source of truth for the build. Hand it to the coding agent. Treat the **Decisions** and **Silence Gating** sections as fixed unless explicitly revisited.

---

## 1. Goal

Record audio playing through the Mac (e.g. a streamed track, a vinyl rip via line-in, anything routed to output) without manually hitting record/stop. The app sits in **listening mode**, starts capturing the instant a signal appears, stops after a configurable run of silence, trims the trailing silence, writes a lossless master, and transcodes a copy to the chosen output format. Each sound event becomes its own file, so a session of several tracks auto-splits on the gaps.

## 2. Non-goals (v1)

- No microphone/voice-call processing, no noise reduction, no normalisation/EQ (post-processing is format conversion only).
- No cloud, no auth, no multi-user. Single local user, single machine.
- No waveform editor UI. A live level meter is enough.
- No packaging/notarisation. It runs via `python -m` / `uvicorn` on localhost.

## 3. Decisions (fixed)

| Concern | Decision | Rationale |
|---|---|---|
| Audio source | **BlackHole 2ch** virtual device + a Multi-Output Device (BlackHole + real output) | Only reliable way to capture system output on macOS while still hearing it |
| Capture lib | Python **`sounddevice`** (PortAudio) | float32 numpy blocks, trivial RMS math, no shelling-out for capture |
| WAV writing | **`soundfile`** | Lossless PCM master, simple API |
| DSP / gating | **numpy** in the capture callback path | Real language for the state machine; ffmpeg filters can't do start-on-signal cleanly |
| Transcode | shell out to **`ffmpeg`** | Industry standard, every codec, trivial to swap bitrate/format |
| Backend | **FastAPI + uvicorn** | Websocket for the meter, simple JSON config endpoints |
| Frontend | Single static **`index.html`** served by FastAPI (vanilla JS + a websocket) | It's a utility panel, not a product UI; no framework needed |
| Config persistence | JSON file (`config.json`) on disk | Human-editable, survives restarts |

**Master master is lossless; the lossy file is a derived copy.** Always keep (or optionally keep) the WAV; transcoding is non-destructive.

## 4. Audio routing (one-time user setup, document in README)

1. Install BlackHole 2ch (`brew install blackhole-2ch`).
2. Open **Audio MIDI Setup** → create a **Multi-Output Device** containing **BlackHole 2ch** + your speakers/headphones.
3. Set the Multi-Output Device as the system output (or route only the source app to BlackHole).
4. In the app, select **BlackHole 2ch** as the input device.

If the user records and the file is silent, it's almost always step 2/3 — surface a hint in the UI.

## 5. Architecture

```
┌──────────────┐   PCM blocks   ┌─────────────────────┐
│ BlackHole 2ch│ ─────────────▶ │ sounddevice callback│
└──────────────┘                │  + ring buffer      │
                                └─────────┬───────────┘
                                          │ level_db per block
                                          ▼
                                ┌─────────────────────┐
                                │  Gate state machine │  LISTENING / RECORDING
                                │  (numpy)            │
                                └─────────┬───────────┘
                          finalise event  │
                                          ▼
                          ┌──────────────────────────┐
                          │ write WAV (soundfile)     │
                          │ → ffmpeg transcode (async)│
                          └──────────────────────────┘

FastAPI: REST config/control + WS pushing {state, level_db, elapsed_ms}
Frontend: meter + controls + config form
```

The capture callback must stay light: compute RMS, push the block + level into a thread-safe queue, and let a worker thread run the state machine and disk I/O. **Never write files or call ffmpeg inside the audio callback** — it runs on a realtime thread and will glitch/drop blocks.

## 6. Silence gating (the core — get this exactly right)

State machine over fixed-size blocks. Default block ≈ 1024 frames @ 48 kHz ≈ 21 ms.

Per block: `rms = sqrt(mean(block**2))`, `level_db = 20*log10(rms + 1e-9)`.

```
STATE = LISTENING

# Always-on: maintain a ring buffer of the last `preroll_ms` of audio.

LISTENING:
    if level_db > start_threshold_db:
        above_ms += block_ms
        if above_ms >= start_debounce_ms:
            buffer = preroll_ring.snapshot()   # prepend captured attack
            silence_ms = 0
            STATE = RECORDING
    else:
        above_ms = 0

RECORDING:
    buffer.append(block)
    if level_db < stop_threshold_db:
        silence_ms += block_ms
        if silence_ms >= silence_stop_ms:
            finalise()
            STATE = LISTENING
    else:
        silence_ms = 0      # reset on any signal (hysteresis)

    # safety cap
    if buffer_duration_ms >= max_recording_ms:
        finalise(); STATE = LISTENING

finalise():
    # trim trailing silence: drop the silence run, keep a small tail
    keep_to = len(buffer) - (silence_samples - tail_keep_samples)
    clip = buffer[:max(keep_to, 0)]
    if clip_duration_ms < min_recording_ms:
        discard()           # ignore clicks/pops that triggered start
        return
    wav_path = write_wav(clip)
    enqueue_transcode(wav_path)   # runs off-thread
```

Non-obvious requirements:

- **Pre-roll buffer** (`preroll_ms`, default 300): the ring buffer runs *always*, even in LISTENING, so the attack isn't clipped. Prepend its snapshot when RECORDING starts.
- **Start debounce** (`start_debounce_ms`, default 150): signal must stay above start threshold this long before triggering, so a single pop doesn't start a recording.
- **Hysteresis:** `stop_threshold_db` should sit a few dB *below* `start_threshold_db` so quiet passages don't flap the gate. Any block above stop threshold resets `silence_ms`.
- **Trailing trim:** keep audio up to the start of the closing silence run, plus `tail_keep_ms` (default 100) so the cut isn't abrupt.
- **Minimum length** (`min_recording_ms`, default 1000): discard sub-second clips — they're almost always false triggers.
- **Safety cap** (`max_recording_ms`, default 900000 = 15 min): force-finalise so a stuck-open gate can't grow the buffer unbounded.
- All thresholds are in **dBFS** (negative numbers, 0 = full scale). Never expose raw linear amplitude in the UI.

## 7. Config schema (`config.json`)

```jsonc
{
  "input_device": "BlackHole 2ch",   // string name or index
  "sample_rate": 48000,              // 44100 | 48000 | 96000
  "bit_depth": 24,                   // 16 | 24  (PCM master)
  "channels": 2,

  "start_threshold_db": -45.0,
  "stop_threshold_db": -50.0,
  "silence_stop_ms": 2000,           // the "X seconds of silence" to stop
  "start_debounce_ms": 150,
  "preroll_ms": 300,
  "tail_keep_ms": 100,
  "min_recording_ms": 1000,
  "max_recording_ms": 900000,

  "output_format": "mp3",            // mp3 | flac | aac | wav(=keep master only)
  "output_bitrate": "320k",          // used by mp3/aac
  "keep_wav_master": true,

  "output_dir": "./recordings",
  "filename_pattern": "{ts}"          // {ts}=YYYYMMDD-HHMMSS, {n}=index
}
```

Every field is editable from the UI form. Validate ranges (e.g. thresholds in [-90, 0], stop < start).

## 8. API surface

```
GET    /api/devices            → [{index, name, max_input_channels, default_samplerate}]
GET    /api/config             → current config
PUT    /api/config             → update (validate, persist, hot-apply where safe)
POST   /api/listen/start       → enter LISTENING
POST   /api/listen/stop        → leave LISTENING (finalise any in-progress recording)
GET    /api/recordings         → [{name, path, duration_ms, size, created, format}]
WS     /ws                     → push every block-ish:
                                  {state, level_db, peak_db, elapsed_ms, silence_ms}
```

Changing `input_device` / `sample_rate` / `bit_depth` requires restarting the stream; do it cleanly on `PUT` while not recording, otherwise queue until LISTENING stops.

## 9. Post-processing (ffmpeg)

Run off the audio thread, ideally a worker queue so several files can transcode without blocking capture.

```bash
# mp3 320
ffmpeg -y -i "$WAV" -codec:a libmp3lame -b:a 320k "$OUT.mp3"
# flac (lossless derived copy)
ffmpeg -y -i "$WAV" -c:a flac "$OUT.flac"
# aac
ffmpeg -y -i "$WAV" -c:a aac -b:a 256k "$OUT.m4a"
```

If `keep_wav_master` is false, delete the WAV after a successful transcode (check exit code 0 first). Detect missing ffmpeg at startup and surface a clear error.

## 10. Project structure

```
silencecut/
├─ backend/
│  ├─ main.py          # FastAPI app, routes, WS, serves frontend
│  ├─ audio.py         # sounddevice stream, ring buffer, gate state machine
│  ├─ transcode.py     # ffmpeg worker queue
│  ├─ config.py        # load/save/validate config.json
│  └─ models.py        # pydantic schemas
├─ frontend/
│  └─ index.html       # meter + controls + config form (vanilla JS)
├─ recordings/         # output (gitignored)
├─ config.json
├─ requirements.txt    # fastapi, uvicorn, sounddevice, soundfile, numpy, pydantic
└─ README.md           # BlackHole setup + run instructions
```

## 11. Build phases (vibe-code in this order — each is independently testable)

1. **Capture proof.** Enumerate devices; open a stream on BlackHole; dump 5s to a WAV. Confirms routing works before any logic exists.
2. **Metering.** Compute `level_db` per block; push over WS; draw a live meter in `index.html`. Confirms the signal path and thresholds you'll actually pick.
3. **Gate.** Implement the LISTENING→RECORDING state machine writing whole-buffer WAVs (no trim yet).
4. **Pre-roll + trailing trim + min-length.** Add the ring buffer, debounce, and trim. This is the "feels right" milestone.
5. **Transcode worker.** WAV → mp3 320 off-thread; `keep_wav_master` handling; recordings list endpoint.
6. **Config UI.** Form bound to `/api/config`, device dropdown, validation, persistence.

## 12. Edge cases / gotchas

- **Silent recordings** → routing misconfig (Multi-Output Device). Show a hint if a finalised clip's peak never exceeded, say, -70 dBFS.
- **Device disappears** mid-session (unplugged interface) → catch PortAudio error, drop to a clear stopped state, don't crash.
- **Sample-rate mismatch** between config and device default → either resample or refuse and report; don't open silently at the wrong rate.
- **ffmpeg not on PATH** → fail fast at startup with install hint (`brew install ffmpeg`).
- **Clipping** in the master → optional warning if any sample hits ±full scale.
- **Don't block the audio callback.** Disk/ffmpeg/WS all happen on other threads via queues.
- **Mono sources** → handle `channels=1` cleanly (RMS over the single channel).

## 13. Acceptance criteria

- Selecting BlackHole and pressing **Listen** does nothing until audio plays.
- Audio starts → a recording begins within ~one pre-roll window, with the attack intact (no clipped start).
- Audio stops → after `silence_stop_ms` the recording finalises, trailing silence trimmed to within `tail_keep_ms`.
- A WAV master (if enabled) **and** a 320 kbps mp3 appear in `output_dir`, correctly named.
- A < `min_recording_ms` blip produces no file.
- All thresholds/durations/format/bitrate are changeable from the UI and persist across restarts.
- The level meter tracks audio in real time with no audible glitches in playback.
