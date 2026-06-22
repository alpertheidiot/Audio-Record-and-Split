# SilenceCut

A local macOS utility app that losslessly captures system audio, automatically triggers recording when a signal is detected, stops and finalises when silence returns, crops trailing silence, saves a master WAV, and transcodes a copy to your preferred target format (MP3, FLAC, AAC) in the background.

## Key Features
- **Hands-off Recording**: Enter **Listening Mode** and let the app split continuous play sessions (like vinyl rips or stream queues) into separate, perfectly-cut files on silence gaps.
- **Pre-Roll Attack Buffer**: Avoid clipped track starts by prepending the last few hundred milliseconds of audio recorded before threshold triggering.
- **Hysteresis Gating**: Separate "Start" and "Stop" thresholds to prevent signal flutter during quiet passages.
- **Ambient Noise Calibration**: Automatically profile system and line-in noise floor to set precise gate boundaries with one click.
- **Level History Visualizer**: View live decibel peaks and tune thresholds interactively on a 15-second rolling timeline chart.
- **In-Browser Audio Player**: Play, download, and manage your captured clips directly in the dashboard.

---

## 1. System Requirements

SilenceCut requires `ffmpeg` for transcoding and `portaudio` for Python audio stream bindings.

### Homebrew Install
```bash
brew install ffmpeg portaudio blackhole-2ch
```

---

## 2. Audio Routing Setup

To capture system audio on macOS without interrupting your hearing, you must create a virtual loopback path:

1. Open **Audio MIDI Setup** (located in Applications -> Utilities).
2. Click the `+` icon in the bottom-left corner and select **Create Multi-Output Device**.
3. In the right panel, check the box next to **BlackHole 2ch** AND your primary speakers or headphones (e.g., *MacBook Pro Speakers* or your DAC).
4. Right-click the **Multi-Output Device** and select **Use This Device For Sound Output**.
5. Keep your macOS volume controls routed to this Multi-Output Device.
6. Open the SilenceCut web panel and select **BlackHole 2ch** (or the Multi-Output Device containing input channels) as your **Audio Input Device**.

*Note: If recordings are silent, verify that your active audio source application is sending sound to the Multi-Output Device.*

---

## 3. Installation & Run

Set up the Python virtual environment and launch the FastAPI server:

```bash
# 1. Create virtual environment
python3 -m venv .venv

# 2. Activate virtual environment
source .venv/bin/activate

# 3. Install Python dependencies
pip install -r requirements.txt

# 4. Start the server on localhost:8000
python3 -m uvicorn backend.main:app --reload
```

Open your browser and navigate to **[http://localhost:8000](http://localhost:8000)**.

---

## 4. macOS Security & Microphone Permissions

On macOS, terminal applications running Python must be granted microphone access to capture incoming audio:

1. Go to **System Settings -> Privacy & Security -> Microphone**.
2. Locate the terminal emulator you are running the command from (e.g. *Terminal*, *iTerm2*, *VS Code*, or *Antigravity*).
3. Toggle the permission switch to **ON**.
4. If the permission prompt was missed or fails, restart your terminal app and start the script again.

---

## 5. Configuration Fields

- **Start Threshold (dB)**: Signal level required to trigger recording.
- **Stop Threshold (dB)**: Signal level required to detect silence. Should be a few dB below the start threshold.
- **Stop Silence (ms)**: How long a silence run must last before saving the recording (e.g., 2000 ms).
- **Debounce (ms)**: Minimum time the signal must stay above the start threshold before recording begins (filters out pops/clicks).
- **Pre-Roll (ms)**: Amount of audio buffered *before* the trigger to prepend to the recording (prevents truncated attacks).
- **Tail Retain (ms)**: Keeps a small tail of the silence run so track fades do not clip abruptly.
- **Min Duration (ms)**: Discards recordings shorter than this threshold (useful to ignore system alert bells).
- **Max Duration (ms)**: Force-closes recording if it exceeds this length (prevents disk space exhaustion).
