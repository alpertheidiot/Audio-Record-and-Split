import os
import shutil
import asyncio
import logging
import subprocess
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Set

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Response
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import ValidationError

from backend.config import load_config, save_config, update_config
from backend.models import AppConfig, DeviceInfo, RecordingInfo
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

def _get_audio_duration_ffprobe(filepath: Path) -> int:
    """Use ffprobe to retrieve duration of audio files in milliseconds."""
    ffprobe_path = shutil.which("ffprobe")
    if not ffprobe_path:
        return 0
        
    cmd = [
        ffprobe_path, 
        "-v", "error", 
        "-show_entries", "format=duration", 
        "-of", "default=noprint_wrappers=1:nokey=1", 
        str(filepath)
    ]
    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=2.0)
        if res.returncode == 0:
            duration_sec = float(res.stdout.strip())
            return int(duration_sec * 1000)
    except Exception as e:
        logger.warning(f"Failed to read duration for {filepath.name} using ffprobe: {e}")
    return 0

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
    
    # Group files by stem so we don't return duplicates of the same track in multiple formats
    # (e.g. track.wav and track.mp3). We prioritize the transcoded copy if available, or WAV if that's the only one.
    stems: Dict[str, List[Path]] = {}
    for entry in out_dir.iterdir():
        if entry.is_file() and entry.suffix.lower() in extensions:
            stem = entry.stem
            if stem not in stems:
                stems[stem] = []
            stems[stem].append(entry)
            
    for stem, paths in stems.items():
        # Find the primary file. We prefer the transcode output format if it exists, or WAV otherwise.
        primary_path = paths[0]
        for path in paths:
            if path.suffix.lower().replace(".", "") == config.output_format:
                primary_path = path
                break
                
        ext = primary_path.suffix.lower().replace(".", "")
        size_bytes = primary_path.stat().st_size
        mtime = primary_path.stat().st_mtime
        created_str = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")
        
        # Read duration
        duration_ms = _get_audio_duration_ffprobe(primary_path)
        
        # Get transcoding status
        status = transcoder.get_status(stem)
        if status == 'unknown':
            status = 'done' # If file exists on disk and is not in queue, it is done
            
        recordings.append({
            "name": stem,
            "path": str(primary_path),
            "duration_ms": duration_ms,
            "size_bytes": size_bytes,
            "created_at": created_str,
            "format": ext,
            "peak_db": 0.0 # Placeholder for simplicity
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
    """Delete a recording stem (deletes all extensions for this file, e.g. wav + mp3)."""
    config = load_config()
    out_dir = Path(config.output_dir)
    if not out_dir.is_absolute():
        out_dir = Path(os.getcwd()) / out_dir
        
    stem = Path(filename).stem
    deleted_any = False
    
    for ext in [".wav", ".mp3", ".flac", ".m4a"]:
        path = out_dir / f"{stem}{ext}"
        if path.exists() and path.is_file():
            try:
                path.unlink()
                deleted_any = True
            except Exception as e:
                logger.error(f"Failed to delete {path}: {e}")
                
    if not deleted_any:
        raise HTTPException(status_code=404, detail="No files found to delete for this recording.")
        
    return {"status": "success", "message": f"Deleted all files for recording '{stem}'"}

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
