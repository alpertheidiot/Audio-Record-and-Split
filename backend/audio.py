import os
import time
import math
import logging
import threading
import queue
from datetime import datetime
from collections import deque
from pathlib import Path
from typing import List, Dict, Union, Optional

import numpy as np
import sounddevice as sd
import soundfile as sf

from backend.config import load_config, save_config, update_config
from backend.models import AppConfig
from backend.transcode import transcoder

logger = logging.getLogger("silencecut.audio")
logger.setLevel(logging.INFO)

# Global status queue to communicate with main.py WebSockets
status_queue = queue.Queue()

class AudioEngine:
    def __init__(self):
        self.config: AppConfig = load_config()
        self.state = "stopped"  # "stopped", "listening", "recording", "calibrating"
        self.stream: Optional[sd.InputStream] = None
        self.gating_thread: Optional[threading.Thread] = None
        
        self.audio_queue: queue.Queue = queue.Queue()
        self.lock = threading.Lock()
        self.running = False
        
        # State machine variables
        self.preroll_ring: deque = deque()
        self.recording_buffer: List[np.ndarray] = []
        self.above_ms = 0.0
        self.silence_ms = 0.0
        self.elapsed_ms = 0.0
        self.peak_db = -90.0
        
        # Calibration state variables
        self.calibration_levels: List[float] = []
        self.calibration_peaks: List[float] = []
        self.calibration_target_ms = 3000.0
        self.calibration_elapsed_ms = 0.0
        self.calibration_callback_result: Optional[dict] = None

    def get_devices(self) -> List[dict]:
        """Enumerate system audio devices."""
        devices = []
        try:
            device_list = sd.query_devices()
            default_input = sd.default.device[0]
            for idx, dev in enumerate(device_list):
                devices.append({
                    "index": idx,
                    "name": dev["name"],
                    "max_input_channels": dev["max_input_channels"],
                    "max_output_channels": dev["max_output_channels"],
                    "default_sample_rate": dev["default_samplerate"],
                    "is_default": idx == default_input
                })
        except Exception as e:
            logger.error(f"Error querying audio devices: {e}")
        return devices

    def _resolve_device_index(self, name_or_idx: Union[str, int]) -> int:
        """Find the PortAudio device index for the configured device."""
        if isinstance(name_or_idx, int):
            return name_or_idx
        
        devices = self.get_devices()
        # Exact match
        for dev in devices:
            if dev["name"] == name_or_idx and dev["max_input_channels"] > 0:
                return dev["index"]
        
        # Partial match
        for dev in devices:
            if name_or_idx.lower() in dev["name"].lower() and dev["max_input_channels"] > 0:
                return dev["index"]
                
        # Default input fallback
        default_input = sd.default.device[0]
        logger.warning(f"Device '{name_or_idx}' not found. Falling back to default device index: {default_input}")
        return default_input

    def start_listening(self) -> str:
        """Enter listening mode. Starts PortAudio stream and worker thread."""
        with self.lock:
            if self.state in ["listening", "recording"]:
                return self.state

            self.config = load_config()  # Load fresh configuration
            self.state = "listening"
            self.running = True
            
            # Reset state variables
            self.recording_buffer = []
            self.above_ms = 0.0
            self.silence_ms = 0.0
            self.elapsed_ms = 0.0
            self.peak_db = -90.0
            
            # Setup pre-roll queue size
            blocksize = 1024
            block_ms = (blocksize / self.config.sample_rate) * 1000.0
            max_preroll_blocks = max(1, int(math.ceil(self.config.preroll_ms / block_ms)))
            self.preroll_ring = deque(maxlen=max_preroll_blocks)
            
            # Clear queues
            while not self.audio_queue.empty():
                try:
                    self.audio_queue.get_nowait()
                except queue.Empty:
                    break
            
            # Start gating worker thread
            self.gating_thread = threading.Thread(target=self._gating_worker, daemon=True, name="AudioGatingWorker")
            self.gating_thread.start()

            # Start PortAudio stream
            device_idx = self._resolve_device_index(self.config.input_device)
            
            try:
                self.stream = sd.InputStream(
                    device=device_idx,
                    channels=self.config.channels,
                    samplerate=self.config.sample_rate,
                    blocksize=blocksize,
                    dtype='float32',
                    callback=self._audio_callback
                )
                self.stream.start()
                logger.info(f"PortAudio stream started on device {device_idx} ({self.config.sample_rate}Hz, {self.config.channels} channels).")
            except Exception as e:
                self.state = "stopped"
                self.running = False
                logger.error(f"Failed to start audio stream: {e}")
                raise RuntimeError(f"Could not open audio stream: {e}. Check device availability and microphone permissions.")
                
            return self.state

    def stop_listening(self) -> str:
        """Leave listening/recording mode. Finalizes any in-progress recording."""
        with self.lock:
            if self.state == "stopped":
                return self.state
            
            was_recording = (self.state == "recording")
            self.running = False
            
            # Stop the stream
            if self.stream:
                try:
                    self.stream.stop()
                    self.stream.close()
                except Exception as e:
                    logger.error(f"Error stopping stream: {e}")
                self.stream = None
                
            # Signal worker thread to exit
            self.audio_queue.put(None)
            
            if self.gating_thread:
                self.gating_thread.join(timeout=2.0)
                self.gating_thread = None

            # If we were recording, finalize the cached audio
            if was_recording and len(self.recording_buffer) > 0:
                logger.info("Stopping engine while recording. Finalizing current clip.")
                self._finalize_recording(forced=True)
                
            self.state = "stopped"
            logger.info("Audio engine stopped.")
            
            # Push final status
            status_queue.put({
                "state": "stopped",
                "level_db": -90.0,
                "peak_db": -90.0,
                "elapsed_ms": 0,
                "silence_ms": 0
            })
            
            return self.state

    def start_calibration(self, duration_sec: float = 3.0) -> str:
        """Start calibration mode to record silence floor for threshold configuration."""
        with self.lock:
            if self.state != "stopped":
                raise RuntimeError(f"Cannot calibrate while engine is in state: {self.state}")
            
            self.config = load_config()
            self.state = "calibrating"
            self.running = True
            
            self.calibration_levels = []
            self.calibration_peaks = []
            self.calibration_target_ms = duration_sec * 1000.0
            self.calibration_elapsed_ms = 0.0
            self.calibration_callback_result = None
            
            blocksize = 1024
            
            # Start gating worker thread
            self.gating_thread = threading.Thread(target=self._gating_worker, daemon=True, name="AudioCalibrationWorker")
            self.gating_thread.start()

            # Start stream
            device_idx = self._resolve_device_index(self.config.input_device)
            try:
                self.stream = sd.InputStream(
                    device=device_idx,
                    channels=self.config.channels,
                    samplerate=self.config.sample_rate,
                    blocksize=blocksize,
                    dtype='float32',
                    callback=self._audio_callback
                )
                self.stream.start()
                logger.info(f"Calibration stream started on device {device_idx}.")
            except Exception as e:
                self.state = "stopped"
                self.running = False
                logger.error(f"Failed to start calibration stream: {e}")
                raise RuntimeError(f"Could not open audio stream for calibration: {e}")
                
            return self.state

    def _audio_callback(self, indata: np.ndarray, frames: int, time_info: dict, status: sd.CallbackFlags):
        """PortAudio real-time audio callback. Must remain highly performant."""
        if not self.running:
            return
            
        if status:
            logger.warning(f"PortAudio status warning: {status}")
            
        # Calculate RMS & Peak levels
        # Avoid division-by-zero/log-zero errors with a safety floor
        rms = np.sqrt(np.mean(indata**2)) if indata.size > 0 else 0.0
        level_db = 20 * np.log10(rms + 1e-9)
        
        peak = np.max(np.abs(indata)) if indata.size > 0 else 0.0
        peak_db = 20 * np.log10(peak + 1e-9)
        
        # Queue raw data block + level metrics for the gating thread to process
        # We copy indata because PortAudio reuses the buffer
        try:
            self.audio_queue.put_nowait((indata.copy(), float(level_db), float(peak_db)))
        except queue.Full:
            logger.warning("Audio queue full! Dropping frame. Reduce CPU load.")

    def _gating_worker(self):
        """Worker thread processing audio queues to execute state transitions & file writes."""
        blocksize = 1024
        block_ms = (blocksize / self.config.sample_rate) * 1000.0
        
        while self.running:
            try:
                item = self.audio_queue.get(timeout=1.0)
                if item is None:
                    break  # Stop signal
                
                block, level_db, peak_db = item
                
                if self.state == "calibrating":
                    self._process_calibration_block(level_db, peak_db, block_ms)
                else:
                    self._process_gating_block(block, level_db, peak_db, block_ms)
                
                self.audio_queue.task_done()
            except queue.Empty:
                continue
            except Exception as e:
                logger.error(f"Error in gating worker thread: {e}", exc_info=True)

    def _process_calibration_block(self, level_db: float, peak_db: float, block_ms: float):
        """Collect levels during calibration mode."""
        self.calibration_levels.append(level_db)
        self.calibration_peaks.append(peak_db)
        self.calibration_elapsed_ms += block_ms
        
        # Broadcast calibration progress
        progress_pct = min(100.0, (self.calibration_elapsed_ms / self.calibration_target_ms) * 100.0)
        status_queue.put({
            "state": "calibrating",
            "level_db": level_db,
            "peak_db": peak_db,
            "elapsed_ms": int(self.calibration_elapsed_ms),
            "silence_ms": 0,
            "progress": progress_pct
        })
        
        if self.calibration_elapsed_ms >= self.calibration_target_ms:
            # Done calibrating
            self.running = False
            
            # Shut down stream
            if self.stream:
                try:
                    self.stream.stop()
                    self.stream.close()
                except Exception as e:
                    logger.error(f"Error closing stream: {e}")
                self.stream = None
                
            # Calculate results
            # Noise floor is average level_db in silence
            avg_noise_db = float(np.mean(self.calibration_levels))
            max_noise_db = float(np.max(self.calibration_levels))
            
            # Recommend thresholds: stop is noise floor + 5dB, start is noise floor + 10dB
            rec_stop = max(-80.0, min(-15.0, max_noise_db + 5.0))
            rec_start = max(-75.0, min(-10.0, max_noise_db + 10.0))
            
            # Make sure start is always at least 3dB above stop
            if rec_start < rec_stop + 3.0:
                rec_start = rec_stop + 3.0
                
            self.calibration_callback_result = {
                "avg_noise_db": avg_noise_db,
                "max_noise_db": max_noise_db,
                "recommended_start_db": rec_start,
                "recommended_stop_db": rec_stop
            }
            
            # Automatically save settings
            self.config.start_threshold_db = round(rec_start, 1)
            self.config.stop_threshold_db = round(rec_stop, 1)
            save_config(self.config)
            
            self.state = "stopped"
            
            status_queue.put({
                "state": "stopped",
                "level_db": -90.0,
                "peak_db": -90.0,
                "elapsed_ms": 0,
                "silence_ms": 0,
                "calibration_result": self.calibration_callback_result
            })
            logger.info(f"Calibration completed. Noise floor max: {max_noise_db:.1f} dB. Settings updated.")

    def _process_gating_block(self, block: np.ndarray, level_db: float, peak_db: float, block_ms: float):
        """Run the core gating state machine."""
        if self.state == "listening":
            # Running buffer of recent audio frames
            self.preroll_ring.append(block)
            
            if level_db > self.config.start_threshold_db:
                self.above_ms += block_ms
                if self.above_ms >= self.config.start_debounce_ms:
                    # Transit to RECORDING
                    self.state = "recording"
                    # Prepopulate buffer with the pre-roll history
                    self.recording_buffer = list(self.preroll_ring)
                    
                    self.silence_ms = 0.0
                    self.elapsed_ms = len(self.recording_buffer) * block_ms
                    self.peak_db = peak_db
                    logger.info("Signal detected. Gating triggered. Recording started.")
            else:
                self.above_ms = 0.0
                
        elif self.state == "recording":
            self.recording_buffer.append(block)
            self.elapsed_ms += block_ms
            self.peak_db = max(self.peak_db, peak_db)
            
            if level_db < self.config.stop_threshold_db:
                self.silence_ms += block_ms
                if self.silence_ms >= self.config.silence_stop_ms:
                    # Transit back to LISTENING
                    logger.info("Silence detected. Stopping recording.")
                    self._finalize_recording(forced=False)
                    self.state = "listening"
                    self.recording_buffer = []
                    self.above_ms = 0.0
            else:
                # Reset silence counter if signal returns (hysteresis)
                self.silence_ms = 0.0
                
            # Safety limit check
            if self.elapsed_ms >= self.config.max_recording_ms:
                logger.info(f"Recording reached max length cap ({self.config.max_recording_ms}ms). Forcing finalization.")
                self._finalize_recording(forced=True)
                self.state = "listening"
                self.recording_buffer = []
                self.above_ms = 0.0

        # Broadcast state updates
        status_queue.put({
            "state": self.state,
            "level_db": level_db,
            "peak_db": self.peak_db if self.state == "recording" else peak_db,
            "elapsed_ms": int(self.elapsed_ms),
            "silence_ms": int(self.silence_ms)
        })

    def _finalize_recording(self, forced: bool = False):
        """Crops silence, verifies durations, writes master WAV, and triggers transcoding."""
        if not self.recording_buffer:
            return

        sample_rate = self.config.sample_rate
        
        # Concatenate blocks to build full raw array
        raw_audio = np.concatenate(self.recording_buffer, axis=0)
        
        # Calculate trailing silence samples to trim
        if forced:
            trimmed_audio = raw_audio
        else:
            # We want to trim the trailing silence except for the tail_keep_ms
            silence_samples = int((self.silence_ms / 1000.0) * sample_rate)
            tail_keep_samples = int((self.config.tail_keep_ms / 1000.0) * sample_rate)
            
            trim_samples = max(0, silence_samples - tail_keep_samples)
            keep_to = len(raw_audio) - trim_samples
            
            trimmed_audio = raw_audio[:max(0, keep_to)]

        # Calculate finalized clip length
        duration_ms = (len(trimmed_audio) / sample_rate) * 1000.0
        
        if duration_ms < self.config.min_recording_ms:
            logger.info(f"Discarding short recording: {duration_ms:.0f}ms (minimum is {self.config.min_recording_ms}ms).")
            return

        # Prepare output directory
        out_dir = Path(self.config.output_dir)
        if not out_dir.is_absolute():
            out_dir = Path(os.getcwd()) / out_dir
        out_dir.mkdir(parents=True, exist_ok=True)

        # Generate filename from pattern
        filename = self._generate_filename(out_dir)
        wav_path = out_dir / f"{filename}.wav"
        
        # WAV master is always saved as 24-bit PCM
        subtype = 'PCM_24'
        
        try:
            sf.write(str(wav_path), trimmed_audio, sample_rate, subtype=subtype)
            logger.info(f"WAV master file saved successfully: {wav_path}")
            
            # Enqueue transcode job
            transcoder.enqueue(
                wav_path=str(wav_path),
                output_dir=str(out_dir),
                output_format=self.config.output_format,
                output_bitrate=self.config.output_bitrate,
                keep_wav_master=self.config.keep_wav_master
            )
            
        except Exception as e:
            logger.error(f"Failed to write audio master file: {e}")

    def _generate_filename(self, output_dir: Path) -> str:
        """Generate filename using the configured format template, avoiding collisions."""
        pattern = self.config.filename_pattern
        ts_str = datetime.now().strftime("%Y%m%d-%H%M%S")
        
        # Simple collision loop
        # Replace template markers
        if "{n}" in pattern:
            n = 1
            while True:
                candidate = pattern.replace("{ts}", ts_str).replace("{n}", str(n))
                # Check if file exists in output directory
                # We check for any of the potential formats (wav, mp3, flac, m4a)
                collision = False
                for ext in [".wav", ".mp3", ".flac", ".m4a"]:
                    if (output_dir / f"{candidate}{ext}").exists():
                        collision = True
                        break
                if not collision:
                    return candidate
                n += 1
        else:
            candidate = pattern.replace("{ts}", ts_str)
            base_candidate = candidate
            n = 1
            while True:
                # If there's a collision on a timestamp only, add an incrementing tag
                collision = False
                for ext in [".wav", ".mp3", ".flac", ".m4a"]:
                    if (output_dir / f"{candidate}{ext}").exists():
                        collision = True
                        break
                if not collision:
                    return candidate
                candidate = f"{base_candidate}-{n}"
                n += 1

# Global audio engine instance
audio_engine = AudioEngine()
