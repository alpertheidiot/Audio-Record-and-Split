import os
import shutil
import subprocess
import threading
import queue
import logging
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger("silencecut.transcode")
logger.setLevel(logging.INFO)

class TranscodeJob:
    def __init__(self, wav_path: str, output_dir: str, output_format: str, output_bitrate: str, keep_wav_master: bool, aac_vbr: bool = True):
        self.wav_path = wav_path
        self.output_dir = output_dir
        self.output_format = output_format.lower().strip()
        self.output_bitrate = output_bitrate
        self.keep_wav_master = keep_wav_master
        self.aac_vbr = aac_vbr
        self.filename = Path(wav_path).stem

class TranscodeQueue:
    def __init__(self):
        self.queue: queue.Queue[TranscodeJob] = queue.Queue()
        self.status: Dict[str, str] = {}  # filename -> status: 'pending', 'transcoding', 'done', 'failed'
        self.lock = threading.Lock()
        self.worker_thread: Optional[threading.Thread] = None
        self.running = False
        
        # Verify ffmpeg presence
        self.ffmpeg_path = shutil.which("ffmpeg")
        if not self.ffmpeg_path:
            logger.error("FFmpeg not found on PATH! Transcoding to MP3/FLAC will fail.")
        else:
            logger.info(f"FFmpeg found at: {self.ffmpeg_path}")
            
        # Verify afconvert presence
        self.afconvert_path = shutil.which("afconvert")
        if not self.afconvert_path:
            logger.error("afconvert not found on PATH! Transcoding to AAC will fail.")
        else:
            logger.info(f"afconvert found at: {self.afconvert_path}")

    def check_ffmpeg(self) -> bool:
        return self.ffmpeg_path is not None

    def start(self):
        with self.lock:
            if self.running:
                return
            self.running = True
            self.worker_thread = threading.Thread(target=self._worker, daemon=True, name="TranscodeWorker")
            self.worker_thread.start()
            logger.info("Transcode worker thread started.")

    def stop(self):
        self.running = False
        self.queue.put(None)  # Sentinel to stop the worker thread
        if self.worker_thread:
            self.worker_thread.join(timeout=2.0)
            logger.info("Transcode worker thread stopped.")

    def enqueue(self, wav_path: str, output_dir: str, output_format: str, output_bitrate: str, keep_wav_master: bool, aac_vbr: bool = True):
        filename = Path(wav_path).stem
        
        with self.lock:
            self.status[filename] = 'pending'
            
        job = TranscodeJob(wav_path, output_dir, output_format, output_bitrate, keep_wav_master, aac_vbr)
        self.queue.put(job)
        logger.info(f"Enqueued transcode job for {filename} (format: {output_format})")

    def get_status(self, filename: str) -> str:
        with self.lock:
            return self.status.get(filename, 'unknown')

    def _worker(self):
        while self.running:
            try:
                job = self.queue.get(timeout=1.0)
                if job is None:
                    break  # Stopping sentinel
                
                self._process_job(job)
                self.queue.task_done()
            except queue.Empty:
                continue
            except Exception as e:
                logger.error(f"Error in transcode worker loop: {e}")

    def _process_job(self, job: TranscodeJob):
        filename = job.filename
        
        with self.lock:
            self.status[filename] = 'transcoding'
            
        logger.info(f"Starting transcode of {filename} to {job.output_format}...")
        
        wav_path = Path(job.wav_path)
        if not wav_path.exists():
            logger.error(f"Cannot transcode: WAV master file does not exist: {wav_path}")
            with self.lock:
                self.status[filename] = 'failed'
            return

        out_dir = Path(job.output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        
        # If output format is WAV, there is no work to do except keep it
        if job.output_format == "wav":
            logger.info(f"Output format is wav. Keeping original master WAV for {filename}.")
            with self.lock:
                self.status[filename] = 'done'
            return

        # Prepare output extension and command
        ext = ""
        cmd = []
        is_afconvert = False
        
        if job.output_format == "mp3":
            ext = ".mp3"
            if not self.ffmpeg_path:
                logger.error("Cannot transcode: ffmpeg executable is missing.")
                with self.lock:
                    self.status[filename] = 'failed'
                return
            cmd = [self.ffmpeg_path, "-y", "-i", str(wav_path), "-vn", "-codec:a", "libmp3lame", "-b:a", job.output_bitrate, str(out_dir / f"{filename}{ext}")]
        elif job.output_format == "flac":
            ext = ".flac"
            if not self.ffmpeg_path:
                logger.error("Cannot transcode: ffmpeg executable is missing.")
                with self.lock:
                    self.status[filename] = 'failed'
                return
            cmd = [self.ffmpeg_path, "-y", "-i", str(wav_path), "-vn", "-c:a", "flac", str(out_dir / f"{filename}{ext}")]
        elif job.output_format == "aac":
            ext = ".m4a"
            afconvert_path = shutil.which("afconvert")
            if not afconvert_path:
                logger.error("Cannot transcode to AAC: afconvert is missing on this system.")
                with self.lock:
                    self.status[filename] = 'failed'
                return
                
            # Parse target bitrate for AAC (only used for CBR)
            br_str = job.output_bitrate.lower().strip()
            if br_str.endswith("k"):
                try:
                    bitrate_bps = int(br_str[:-1]) * 1000
                except ValueError:
                    bitrate_bps = 256000
            else:
                try:
                    bitrate_bps = int(br_str)
                    if bitrate_bps < 1000:
                        bitrate_bps *= 1000
                except ValueError:
                    bitrate_bps = 256000
                    
            if job.aac_vbr:
                # VBR allocation strategy (strategy 3, quality 127)
                cmd = [
                    afconvert_path,
                    "-f", "m4af",
                    "-d", "aac",
                    "-s", "3",
                    "-u", "vbrq", "127",
                    str(wav_path),
                    str(out_dir / f"{filename}{ext}")
                ]
            else:
                # CBR allocation strategy (strategy 0, quality 127)
                cmd = [
                    afconvert_path,
                    "-f", "m4af",
                    "-d", "aac",
                    "-q", "127",
                    "-s", "0",
                    "-b", str(bitrate_bps),
                    str(wav_path),
                    str(out_dir / f"{filename}{ext}")
                ]
            is_afconvert = True
        else:
            logger.error(f"Unsupported output format: {job.output_format}")
            with self.lock:
                self.status[filename] = 'failed'
            return

        out_path = out_dir / f"{filename}{ext}"
        
        try:
            # Shell out to transcoder
            transcoder_name = "afconvert" if is_afconvert else "ffmpeg"
            logger.info(f"Running {transcoder_name} command: {' '.join(cmd)}")
            result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
            
            logger.info(f"Transcoding completed successfully for {filename} -> {out_path.name}")
            
            # Post-transcode cleanup
            if not job.keep_wav_master:
                logger.info(f"Deleting raw WAV master: {wav_path}")
                try:
                    wav_path.unlink()
                except Exception as ex:
                    logger.error(f"Failed to delete WAV master {wav_path}: {ex}")
            
            with self.lock:
                self.status[filename] = 'done'
                
            # Auto-identify if enabled
            try:
                from backend.config import load_config
                config = load_config()
                if config.acoustid_enabled:
                    logger.info(f"Auto-identifying transcoded file: {out_path}")
                    from backend.acoustid import identify_file
                    ident_res = identify_file(
                        filepath=str(out_path),
                        api_key=config.acoustid_api_key,
                        confidence_threshold=config.acoustid_confidence_threshold
                    )
                    if ident_res:
                        logger.info(f"Auto-identification succeeded: {ident_res['artist']} - {ident_res['title']}")
                        from backend.audio import status_queue, audio_engine
                        status_queue.put({
                            "state": audio_engine.state,
                            "level_db": -90.0,
                            "peak_db": -90.0,
                            "elapsed_ms": 0,
                            "silence_ms": 0,
                            "identified": True,
                            "original_name": filename,
                            "new_name": Path(ident_res["new_path"]).stem,
                            "artist": ident_res["artist"],
                            "title": ident_res["title"]
                        })
            except Exception as ident_err:
                logger.error(f"Error during auto-identification for {filename}: {ident_err}", exc_info=True)
                
        except subprocess.CalledProcessError as e:
            logger.error(f"ffmpeg failed for {filename}. Exit code: {e.returncode}. Stderr: {e.stderr}")
            with self.lock:
                self.status[filename] = 'failed'
        except Exception as e:
            logger.error(f"Unexpected error transcoding {filename}: {e}")
            with self.lock:
                self.status[filename] = 'failed'

# Global transcoder instance
transcoder = TranscodeQueue()
