import os
import shutil
import asyncio
import logging
import subprocess
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Set

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Response, BackgroundTasks
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import ValidationError

from backend.config import load_config, save_config, update_config
from backend.models import AppConfig, DeviceInfo, RecordingInfo, ConvertRequest
from backend.audio import audio_engine, status_queue
from backend.transcode import transcoder

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("silencecut.main")

app = FastAPI(title="SilenceCut Audio Capture")

# Active WebSocket connections
active_connections: Set[WebSocket] = set()
broadcast_task: Optional[asyncio.Task] = None

async def ws_broadcast_worker():
    """Background task pulling status updates from the audio engine queue and broadcasting to WebSockets."""
    logger.info("WebSocket broadcast worker started.")
    while True:
        try:
            # Poll status queue in a non-blocking thread execution
            status_data = await asyncio.to_thread(status_queue.get)
            if status_data is None:
                break
                
            # Broadcast JSON status to all connected websockets
            if active_connections:
                disconnected = set()
                for ws in active_connections:
                    try:
                        await ws.send_json(status_data)
                    except Exception:
                        disconnected.add(ws)
                for ws in disconnected:
                    active_connections.discard(ws)
                    
            status_queue.task_done()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Error in WebSocket broadcast worker: {e}", exc_info=True)
            await asyncio.sleep(0.5)

@app.on_event("startup")
async def startup_event():
    global broadcast_task
    # Start transcode queue
    transcoder.start()
    
    # Start WebSocket broadcaster
    broadcast_task = asyncio.create_task(ws_broadcast_worker())
    logger.info("FastAPI application startup complete.")

@app.on_event("shutdown")
async def shutdown_event():
    global broadcast_task
    # Stop audio engine
    try:
        audio_engine.stop_listening()
    except Exception as e:
        logger.error(f"Error stopping audio engine on shutdown: {e}")
        
    # Stop transcode queue
    transcoder.stop()
    
    # Stop WebSocket broadcaster
    if broadcast_task:
        broadcast_task.cancel()
        try:
            await broadcast_task
        except asyncio.CancelledError:
            pass
            
    # Push stop sentinel to status queue
    status_queue.put(None)
    logger.info("FastAPI application shutdown complete.")

# ----------------- REST API ENDPOINTS -----------------

@app.get("/", response_class=HTMLResponse)
async def get_index():
    """Serve the single-page frontend HTML."""
    html_path = Path("frontend/index.html")
    if not html_path.exists():
        raise HTTPException(status_code=404, detail="frontend/index.html not found.")
    with open(html_path, "r") as f:
        return f.read()

@app.get("/api/devices", response_model=List[DeviceInfo])
def get_devices():
    """List available PortAudio input and output devices."""
    try:
        return audio_engine.get_devices()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/config", response_model=AppConfig)
def get_config():
    """Get current application configuration."""
    return load_config()

@app.put("/api/config", response_model=AppConfig)
def put_config(new_config: dict):
    """Update and persist application configuration. Performs stream hot-reload if safe."""
    current_config = load_config()
    current_state = audio_engine.state
    
    # Check if hardware-critical settings are changing
    hw_settings = ["input_device", "sample_rate", "channels"]
    hw_changed = any(new_config.get(k) != getattr(current_config, k) for k in hw_settings if k in new_config)
    
    if hw_changed:
        if current_state == "recording":
            raise HTTPException(
                status_code=400, 
                detail="Cannot update hardware settings (device, sample rate, channels) while actively recording."
            )
        elif current_state == "listening":
            logger.info("Hardware config changed while listening. Reopening stream...")
            audio_engine.stop_listening()
            try:
                updated = update_config(new_config)
                audio_engine.start_listening()
                return updated
            except Exception as e:
                raise HTTPException(status_code=400, detail=f"Failed to apply config: {e}")
                
    try:
        updated = update_config(new_config)
        # Hot-apply non-hardware config changes to the running audio engine
        audio_engine.config = updated
        return updated
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=e.errors())
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/listen/start")
def start_listen():
    """Enter listening mode."""
    try:
        state = audio_engine.start_listening()
        return {"status": "success", "state": state}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/listen/stop")
def stop_listen():
    """Stop listening (finalizing any recording)."""
    try:
        state = audio_engine.stop_listening()
        return {"status": "success", "state": state}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/listen/calibrate")
def start_calibrate(duration_sec: float = 3.0):
    """Trigger ambient silence noise calibration."""
    try:
        state = audio_engine.start_calibration(duration_sec=duration_sec)
        return {"status": "success", "state": state}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

def _get_audio_info_ffprobe(filepath: Path) -> Dict[str, int]:
    """Use ffprobe to retrieve duration in ms and bitrate in kbps."""
    ffprobe_path = shutil.which("ffprobe")
    info = {"duration_ms": 0, "bitrate_kbps": 0}
    if not ffprobe_path:
        return info
        
    cmd = [
        ffprobe_path, 
        "-v", "error", 
        "-show_entries", "format=duration,bit_rate", 
        "-of", "default=noprint_wrappers=1", 
        str(filepath)
    ]
    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=2.0)
        if res.returncode == 0:
            for line in res.stdout.strip().split("\n"):
                if "=" in line:
                    key, val = line.split("=", 1)
                    val = val.strip()
                    if key == "duration" and val != "N/A" and val:
                        info["duration_ms"] = int(float(val) * 1000)
                    elif key == "bit_rate" and val != "N/A" and val:
                        info["bitrate_kbps"] = int(int(val) / 1000)
    except Exception as e:
        logger.warning(f"Failed to read audio info for {filepath.name} using ffprobe: {e}")
        
    if info["duration_ms"] > 0 and info["bitrate_kbps"] == 0:
        try:
            size_bytes = filepath.stat().st_size
            duration_sec = info["duration_ms"] / 1000.0
            info["bitrate_kbps"] = int((size_bytes * 8) / duration_sec / 1000.0)
        except Exception:
            pass
            
    return info

@app.get("/api/recordings", response_model=List[RecordingInfo])
def list_recordings():
    """List all recorded clips in the output folder."""
    config = load_config()
    out_dir = Path(config.output_dir)
    
    if not out_dir.is_absolute():
        out_dir = Path(os.getcwd()) / out_dir
        
    if not out_dir.exists():
        return []
        
    recordings = []
    
    # We want to scan files that match our output extension formats
    extensions = [".wav", ".mp3", ".flac", ".m4a"]
    
    for entry in out_dir.iterdir():
        if entry.is_file() and entry.suffix.lower() in extensions:
            stem = entry.stem
            ext = entry.suffix.lower().replace(".", "")
            size_bytes = entry.stat().st_size
            mtime = entry.stat().st_mtime
            created_str = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")
            
            # Read duration and bitrate
            info = _get_audio_info_ffprobe(entry)
            
            # Read metadata tags using mutagen
            artist = None
            title = None
            album = None
            has_cover_art = False
            
            try:
                from mutagen import File as MutagenFile
                audio_tags = MutagenFile(entry)
                if audio_tags:
                    if entry.suffix.lower() == ".mp3":
                        if "TPE1" in audio_tags:
                            artist = str(audio_tags["TPE1"])
                        if "TIT2" in audio_tags:
                            title = str(audio_tags["TIT2"])
                        if "TALB" in audio_tags:
                            album = str(audio_tags["TALB"])
                        from mutagen.id3 import APIC
                        for tag in audio_tags.values():
                            if isinstance(tag, APIC):
                                has_cover_art = True
                                break
                    elif entry.suffix.lower() == ".flac":
                        artist = audio_tags.get("artist", [None])[0]
                        title = audio_tags.get("title", [None])[0]
                        album = audio_tags.get("album", [None])[0]
                        if audio_tags.pictures:
                            has_cover_art = True
                    elif entry.suffix.lower() in [".m4a", ".mp4"]:
                        artist = audio_tags.get('\xa9ART', [None])[0]
                        title = audio_tags.get('\xa9nam', [None])[0]
                        album = audio_tags.get('\xa9alb', [None])[0]
                        if "covr" in audio_tags:
                            has_cover_art = True
                    elif entry.suffix.lower() == ".wav":
                        if hasattr(audio_tags, "tags") and audio_tags.tags:
                            if "TPE1" in audio_tags.tags:
                                artist = str(audio_tags.tags["TPE1"])
                            if "TIT2" in audio_tags.tags:
                                title = str(audio_tags.tags["TIT2"])
                            if "TALB" in audio_tags.tags:
                                album = str(audio_tags.tags["TALB"])
                            from mutagen.id3 import APIC
                            for tag in audio_tags.tags.values():
                                if isinstance(tag, APIC):
                                    has_cover_art = True
                                    break
            except Exception as tag_err:
                logger.debug(f"Failed to read tags for {entry.name}: {tag_err}")
                
            recordings.append({
                "name": stem,
                "path": str(entry),
                "duration_ms": info["duration_ms"],
                "size_bytes": size_bytes,
                "created_at": created_str,
                "format": ext,
                "peak_db": 0.0,
                "bitrate_kbps": info["bitrate_kbps"],
                "artist": artist,
                "title": title,
                "album": album,
                "has_cover_art": has_cover_art
            })
        
    # Sort by created time descending
    recordings.sort(key=lambda x: x["created_at"], reverse=True)
    return recordings

@app.get("/api/recordings/download/{filename}")
def download_recording(filename: str):
    """Download a specific recording file."""
    config = load_config()
    out_dir = Path(config.output_dir)
    if not out_dir.is_absolute():
        out_dir = Path(os.getcwd()) / out_dir
        
    filepath = out_dir / filename
    if not filepath.exists() or not filepath.is_file():
        raise HTTPException(status_code=404, detail="Audio file not found")
        
    return FileResponse(path=str(filepath), filename=filename)

@app.delete("/api/recordings/{filename}")
def delete_recording(filename: str):
    """Delete a specific recording file."""
    config = load_config()
    out_dir = Path(config.output_dir)
    if not out_dir.is_absolute():
        out_dir = Path(os.getcwd()) / out_dir
        
    filepath = out_dir / filename
    if filepath.exists() and filepath.is_file():
        try:
            filepath.unlink()
            return {"status": "success", "message": f"Deleted file '{filename}'"}
        except Exception as e:
            logger.error(f"Failed to delete {filepath}: {e}")
            raise HTTPException(status_code=500, detail=f"Failed to delete file: {e}")
    else:
        raise HTTPException(status_code=404, detail="File not found")

@app.post("/api/recordings/{filename}/identify")
def identify_recording(filename: str):
    """Run AcoustID identification on the specified file."""
    config = load_config()
    out_dir = Path(config.output_dir)
    if not out_dir.is_absolute():
        out_dir = Path(os.getcwd()) / out_dir
        
    filepath = out_dir / filename
    if not filepath.exists() or not filepath.is_file():
        raise HTTPException(status_code=404, detail="Audio file not found")
        
    from backend.acoustid import identify_file
    try:
        result = identify_file(
            filepath=str(filepath),
            api_key=config.acoustid_api_key,
            confidence_threshold=config.acoustid_confidence_threshold
        )
        if result:
            return {
                "status": "success",
                "message": "Track identified successfully",
                "artist": result["artist"],
                "title": result["title"],
                "new_filename": Path(result["new_path"]).name
            }
        else:
            raise HTTPException(status_code=404, detail="No matches found with sufficient confidence")
    except Exception as e:
        logger.error(f"Manual identification failed for {filename}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/recordings/{filename}/coverart")
def get_coverart(filename: str):
    """Serve the cover art embedded in the audio file."""
    config = load_config()
    out_dir = Path(config.output_dir)
    if not out_dir.is_absolute():
        out_dir = Path(os.getcwd()) / out_dir
        
    filepath = out_dir / filename
    if not filepath.exists() or not filepath.is_file():
        raise HTTPException(status_code=404, detail="Audio file not found")
        
    ext = filepath.suffix.lower()
    cover_data = None
    mime = "image/jpeg"
    
    try:
        if ext == ".mp3":
            from mutagen.mp3 import MP3
            from mutagen.id3 import APIC
            audio = MP3(filepath)
            if audio.tags:
                for tag in audio.tags.values():
                    if isinstance(tag, APIC):
                        cover_data = tag.data
                        mime = tag.mime
                        break
        elif ext == ".flac":
            from mutagen.flac import FLAC
            audio = FLAC(filepath)
            if audio.pictures:
                cover_data = audio.pictures[0].data
                mime = audio.pictures[0].mime
        elif ext in [".m4a", ".mp4"]:
            from mutagen.mp4 import MP4, MP4Cover
            audio = MP4(filepath)
            if "covr" in audio:
                covr = audio["covr"][0]
                cover_data = bytes(covr)
                if getattr(covr, 'imageformat', None) == MP4Cover.FORMAT_PNG:
                    mime = "image/png"
        elif ext == ".wav":
            from mutagen.wave import WAVE
            from mutagen.id3 import APIC
            audio = WAVE(filepath)
            if audio.tags:
                for tag in audio.tags.values():
                    if isinstance(tag, APIC):
                        cover_data = tag.data
                        mime = tag.mime
                        break
    except Exception as e:
        logger.error(f"Error reading cover art from {filename}: {e}")
        raise HTTPException(status_code=500, detail="Failed to read cover art from file")
        
    if not cover_data:
        raise HTTPException(status_code=404, detail="No cover art found in this file")
        
    return Response(content=cover_data, media_type=mime)

def _run_ffmpeg_convert(cmd: List[str], target_filename: str):
    try:
        logger.info(f"Background convert running ffmpeg: {' '.join(cmd)}")
        subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
        logger.info(f"Background convert succeeded for {target_filename}")
        status_queue.put({
            "state": audio_engine.state,
            "level_db": -90.0,
            "peak_db": -90.0,
            "elapsed_ms": 0,
            "silence_ms": 0,
            "conversion_completed": True,
            "filename": target_filename
        })
    except Exception as e:
        logger.error(f"Background convert failed for {target_filename}: {e}")
        status_queue.put({
            "state": audio_engine.state,
            "level_db": -90.0,
            "peak_db": -90.0,
            "elapsed_ms": 0,
            "silence_ms": 0,
            "conversion_failed": True,
            "filename": target_filename
        })

@app.post("/api/recordings/{filename}/convert")
def convert_recording(filename: str, request: ConvertRequest, background_tasks: BackgroundTasks):
    """Transcode a specific recording to another format in the background."""
    config = load_config()
    out_dir = Path(config.output_dir)
    if not out_dir.is_absolute():
        out_dir = Path(os.getcwd()) / out_dir
        
    src_path = out_dir / filename
    if not src_path.exists() or not src_path.is_file():
        raise HTTPException(status_code=404, detail="Source audio file not found")
        
    target_format = request.target_format.lower().strip()
    if target_format not in ["mp3", "flac", "aac", "wav"]:
        raise HTTPException(status_code=400, detail="Invalid target format")
        
    ext = {
        "mp3": ".mp3",
        "flac": ".flac",
        "aac": ".m4a",
        "wav": ".wav"
    }[target_format]
    
    if src_path.suffix.lower() == ext or (src_path.suffix.lower() == ".m4a" and target_format == "aac"):
        raise HTTPException(status_code=400, detail="Source file is already in the target format")
        
    stem = src_path.stem
    candidate = f"{stem}{ext}"
    counter = 1
    while (out_dir / candidate).exists():
        candidate = f"{stem}-{counter}{ext}"
        counter += 1
        
    dest_path = out_dir / candidate
    
    if target_format == "aac":
        afconvert_path = shutil.which("afconvert")
        if not afconvert_path:
            raise HTTPException(status_code=500, detail="afconvert not found on the system path")
            
        br_str = (request.bitrate or "256k").lower().strip()
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
                
        if request.aac_vbr:
            cmd = [
                afconvert_path,
                "-f", "m4af",
                "-d", "aac",
                "-s", "3",
                "-u", "vbrq", "127",
                str(src_path),
                str(dest_path)
            ]
        else:
            cmd = [
                afconvert_path,
                "-f", "m4af",
                "-d", "aac",
                "-q", "127",
                "-s", "0",
                "-b", str(bitrate_bps),
                str(src_path),
                str(dest_path)
            ]
    else:
        ffmpeg_path = shutil.which("ffmpeg")
        if not ffmpeg_path:
            raise HTTPException(status_code=500, detail="FFmpeg not found on the system path")
            
        ffmpeg_args = []
        if target_format == "mp3":
            ffmpeg_args = ["-codec:a", "libmp3lame", "-b:a", request.bitrate or "320k"]
        elif target_format == "flac":
            ffmpeg_args = ["-c:a", "flac"]
        elif target_format == "wav":
            ffmpeg_args = ["-c:a", "pcm_s16le"]
            
        cmd = [ffmpeg_path, "-y", "-i", str(src_path), "-vn"] + ffmpeg_args + [str(dest_path)]
        
    background_tasks.add_task(_run_ffmpeg_convert, cmd, candidate)
    
    return {
        "status": "success", 
        "message": f"Conversion to {target_format.upper()} started in background.", 
        "target_file": candidate
    }



@app.post("/api/system/stop")
def stop_services():
    """Stop all services by killing the process on port 8000."""
    logger.info("System stop requested.")
    def kill_task():
        import time
        import subprocess
        time.sleep(0.5)  # Give FastAPI time to send the response
        # Use -s TCP:LISTEN to avoid killing client processes (like the browser) connected to port 8000
        subprocess.run("kill -9 $(lsof -t -i :8000 -s TCP:LISTEN)", shell=True)
        
    import threading
    threading.Thread(target=kill_task, daemon=True).start()
    return {"status": "success", "message": "Stopping all services..."}

# ----------------- WEBSOCKETS -----------------

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket endpoint pushing real-time decibel level updates and system state."""
    await websocket.accept()
    active_connections.add(websocket)
    
    # Send initial config/state on connect
    try:
        await websocket.send_json({
            "state": audio_engine.state,
            "level_db": -90.0,
            "peak_db": -90.0,
            "elapsed_ms": 0,
            "silence_ms": 0,
            "connected": True
        })
        
        # Keep connection open
        while True:
            # We just wait for incoming client pings or disconnection
            await websocket.receive_text()
            
    except WebSocketDisconnect:
        logger.info("WebSocket client disconnected.")
    except Exception as e:
        logger.error(f"WebSocket connection error: {e}")
    finally:
        active_connections.discard(websocket)
