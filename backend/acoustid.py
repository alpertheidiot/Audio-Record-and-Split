import os
import re
import subprocess
import json
import shutil
import logging
from pathlib import Path
import urllib.request
import urllib.parse
import urllib.error
from PIL import Image
import io
from typing import Tuple, Optional

logger = logging.getLogger("silencecut.acoustid")
logger.setLevel(logging.INFO)

def get_fingerprint(filepath: str) -> Tuple[int, str]:
    """Generates the Chromaprint fingerprint using the fpcalc tool."""
    fpcalc_path = shutil.which("fpcalc") or "/opt/homebrew/bin/fpcalc"
    if not Path(fpcalc_path).exists():
        raise FileNotFoundError(f"fpcalc executable not found. Searched PATH and /opt/homebrew/bin/fpcalc")
        
    cmd = [fpcalc_path, "-json", filepath]
    logger.info(f"Running fpcalc: {' '.join(cmd)}")
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
    data = json.loads(res.stdout)
    # duration might be float, convert to int seconds
    return int(float(data["duration"])), data["fingerprint"]

def lookup_acoustid(duration: int, fingerprint: str, api_key: str, confidence_threshold: float) -> Optional[dict]:
    """Queries the AcoustID API for track matching data."""
    if not api_key:
        raise ValueError("AcoustID API Key is missing. Please configure it in Settings.")
        
    url = "https://api.acoustid.org/v2/lookup"
    params = {
        "format": "json",
        "client": api_key,
        "meta": "recordings releases",
        "duration": duration,
        "fingerprint": fingerprint
    }
    try:
        data = urllib.parse.urlencode(params).encode("utf-8")
        req = urllib.request.Request(url, data=data)
        req.add_header("User-Agent", "SilenceCut/1.0")
        
        with urllib.request.urlopen(req, timeout=10) as response:
            res = json.loads(response.read().decode("utf-8"))
            
        if res.get("status") != "ok":
            err_msg = res.get("error", {}).get("message", "Unknown error")
            logger.error(f"AcoustID lookup failed with status: {res.get('status')} - {err_msg}")
            raise ValueError(f"AcoustID API Error: {err_msg}")
            
        results = res.get("results", [])
        if not results:
            logger.info("No AcoustID results returned.")
            return None
            
        # Find results that are above the confidence threshold
        best_match = None
        for result in results:
            score = result.get("score", 0.0)
            if score >= confidence_threshold:
                best_match = result
                break
                
        if not best_match:
            logger.info(f"No AcoustID matches met the confidence threshold of {confidence_threshold}")
            return None
            
        recordings = best_match.get("recordings", [])
        if not recordings:
            logger.info("No recordings found in the best match result.")
            return None
            
        # Return the first recording details
        recording = recordings[0]
        title = recording.get("title")
        artists = recording.get("artists", [])
        artist_name = ", ".join([a.get("name", "") for a in artists if a.get("name")]) if artists else "Unknown Artist"
        
        # Get MBID of the first release to fetch cover art later
        release_mbid = None
        album_title = None
        releases = recording.get("releases", [])
        if releases:
            for r in releases:
                if r.get("id"):
                    release_mbid = r.get("id")
                    album_title = r.get("title")
                    break
                    
        return {
            "title": title,
            "artist": artist_name,
            "album": album_title,
            "release_mbid": release_mbid,
            "score": best_match.get("score", 0.0)
        }
    except urllib.error.HTTPError as he:
        err_msg = he.reason
        try:
            error_data = json.loads(he.read().decode("utf-8"))
            if isinstance(error_data, dict) and "error" in error_data:
                err_msg = error_data["error"].get("message", he.reason)
        except Exception as parse_err:
            logger.error(f"Failed to parse AcoustID error response: {parse_err}")
            
        logger.error(f"AcoustID API returned HTTP {he.code}: {err_msg}")
        raise ValueError(f"AcoustID API Error: {err_msg}")
    except urllib.error.URLError as ue:
        logger.error(f"Failed to connect to AcoustID server: {ue.reason}")
        raise ValueError(f"Failed to connect to AcoustID server: {ue.reason}")
    except Exception as e:
        if isinstance(e, ValueError):
            raise
        logger.error(f"Error calling AcoustID API: {e}")
        raise ValueError(f"Error calling AcoustID API: {e}")

def fetch_cover_art(mbid: str) -> Optional[bytes]:
    """Queries the Cover Art Archive API for the given MBID to download front cover image bytes."""
    if not mbid:
        return None
    url = f"https://coverartarchive.org/release/{mbid}"
    try:
        req = urllib.request.Request(url)
        req.add_header("User-Agent", "SilenceCut/1.0 ( alper@example.com )")
        with urllib.request.urlopen(req, timeout=10) as response:
            metadata = json.loads(response.read().decode("utf-8"))
            
        images = metadata.get("images", [])
        if not images:
            return None
            
        selected_img_url = None
        for img in images:
            if img.get("front"):
                selected_img_url = img.get("image")
                break
        if not selected_img_url:
            selected_img_url = images[0].get("image")
            
        if not selected_img_url:
            return None
            
        logger.info(f"Downloading cover art from: {selected_img_url}")
        img_req = urllib.request.Request(selected_img_url)
        img_req.add_header("User-Agent", "SilenceCut/1.0 ( alper@example.com )")
        with urllib.request.urlopen(img_req, timeout=15) as img_response:
            return img_response.read()
    except urllib.error.HTTPError as he:
        if he.code == 404:
            logger.info(f"No cover art found in Cover Art Archive for release MBID: {mbid}")
        else:
            logger.error(f"HTTP error fetching cover art metadata: {he}")
        return None
    except Exception as e:
        logger.error(f"Error fetching cover art: {e}")
        return None

def resize_image(img_bytes: bytes) -> Optional[bytes]:
    """Crop and resize the cover image to square dimensions between 600x600 and 800x800."""
    if not img_bytes:
        return None
    try:
        img = Image.open(io.BytesIO(img_bytes))
        if img.mode != 'RGB':
            img = img.convert('RGB')
            
        width, height = img.size
        
        # Crop center square if dimensions are different
        if width != height:
            min_dim = min(width, height)
            left = (width - min_dim) / 2
            top = (height - min_dim) / 2
            right = (width + min_dim) / 2
            bottom = (height + min_dim) / 2
            img = img.crop((left, top, right, bottom))
            
        # Target sizing range: [600, 800]
        size = img.width
        if size > 800:
            size = 800
        elif size < 600:
            size = 600
            
        if size != img.width:
            logger.info(f"Resizing cover art from {img.width}x{img.height} to {size}x{size}")
            img = img.resize((size, size), Image.Resampling.LANCZOS)
            
        out_io = io.BytesIO()
        img.save(out_io, format="JPEG", quality=90)
        return out_io.getvalue()
    except Exception as e:
        logger.error(f"Error processing/resizing image: {e}")
        return None

def write_tags(filepath: str, title: str, artist: str, album: Optional[str] = None, cover_bytes: Optional[bytes] = None):
    """Writes title, artist, album, and front cover art metadata to the audio file."""
    ext = Path(filepath).suffix.lower()
    
    if ext == ".mp3":
        from mutagen.mp3 import MP3
        from mutagen.id3 import ID3, APIC, TPE1, TIT2, TALB, error
        try:
            audio = MP3(filepath, ID3=ID3)
            try:
                audio.add_tags()
            except error:
                pass
            
            audio.tags.add(TPE1(encoding=3, text=artist))
            audio.tags.add(TIT2(encoding=3, text=title))
            if album:
                audio.tags.add(TALB(encoding=3, text=album))
                
            if cover_bytes:
                audio.tags.add(APIC(
                    encoding=3,
                    mime='image/jpeg',
                    type=3, # front cover
                    desc=u'Cover',
                    data=cover_bytes
                ))
            audio.save()
            logger.info(f"Successfully tagged MP3 file: {filepath}")
        except Exception as e:
            logger.error(f"Failed to tag MP3 file {filepath}: {e}")
            
    elif ext == ".flac":
        from mutagen.flac import FLAC, Picture
        try:
            audio = FLAC(filepath)
            audio["artist"] = artist
            audio["title"] = title
            if album:
                audio["album"] = album
                
            if cover_bytes:
                pic = Picture()
                pic.data = cover_bytes
                pic.type = 3
                pic.mime = "image/jpeg"
                pic.description = "Cover"
                audio.clear_pictures()
                audio.add_picture(pic)
            audio.save()
            logger.info(f"Successfully tagged FLAC file: {filepath}")
        except Exception as e:
            logger.error(f"Failed to tag FLAC file {filepath}: {e}")
            
    elif ext in [".m4a", ".mp4"]:
        from mutagen.mp4 import MP4, MP4Cover
        try:
            audio = MP4(filepath)
            audio['\xa9ART'] = artist
            audio['\xa9nam'] = title
            if album:
                audio['\xa9alb'] = album
                
            if cover_bytes:
                audio['covr'] = [MP4Cover(cover_bytes, imageformat=MP4Cover.FORMAT_JPEG)]
            audio.save()
            logger.info(f"Successfully tagged M4A/AAC file: {filepath}")
        except Exception as e:
            logger.error(f"Failed to tag M4A file {filepath}: {e}")
            
    elif ext == ".wav":
        from mutagen.wave import WAVE
        from mutagen.id3 import ID3, APIC, TPE1, TIT2, TALB, error
        try:
            audio = WAVE(filepath)
            try:
                audio.add_tags()
            except error:
                pass
            
            audio.tags.add(TPE1(encoding=3, text=artist))
            audio.tags.add(TIT2(encoding=3, text=title))
            if album:
                audio.tags.add(TALB(encoding=3, text=album))
                
            if cover_bytes:
                audio.tags.add(APIC(
                    encoding=3,
                    mime='image/jpeg',
                    type=3,
                    desc=u'Cover',
                    data=cover_bytes
                ))
            audio.save()
            logger.info(f"Successfully tagged WAV file: {filepath}")
        except Exception as e:
            logger.error(f"Failed to tag WAV file {filepath}: {e}")

def sanitize_filename(name: str) -> str:
    """Removes filesystem-unsafe characters from a string."""
    name = re.sub(r'[\\/*?:"<>|]', "", name)
    name = re.sub(r'\s+', " ", name)
    return name.strip()

HISTORY_FILE = ".naming_history.json"

def load_naming_history(output_dir: Path) -> dict:
    history_path = output_dir / HISTORY_FILE
    if not history_path.exists():
        return {}
    try:
        with open(history_path, "r") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Error loading naming history: {e}")
        return {}

def save_naming_history(output_dir: Path, history: dict):
    history_path = output_dir / HISTORY_FILE
    try:
        with open(history_path, "w") as f:
            json.dump(history, f, indent=2)
    except Exception as e:
        logger.error(f"Error saving naming history: {e}")

def lookup_acoustid_candidates(duration: int, fingerprint: str, api_key: str) -> list:
    """Queries the AcoustID API and returns all match candidates."""
    if not api_key:
        raise ValueError("AcoustID API Key is missing. Please configure it in Settings.")
        
    url = "https://api.acoustid.org/v2/lookup"
    params = {
        "format": "json",
        "client": api_key,
        "meta": "recordings releases",
        "duration": duration,
        "fingerprint": fingerprint
    }
    
    try:
        data = urllib.parse.urlencode(params).encode("utf-8")
        req = urllib.request.Request(url, data=data)
        req.add_header("User-Agent", "SilenceCut/1.0")
        
        with urllib.request.urlopen(req, timeout=10) as response:
            res = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as he:
        err_msg = he.reason
        try:
            error_data = json.loads(he.read().decode("utf-8"))
            if isinstance(error_data, dict) and "error" in error_data:
                err_msg = error_data["error"].get("message", he.reason)
        except Exception:
            pass
        logger.error(f"AcoustID API returned HTTP {he.code}: {err_msg}")
        raise ValueError(f"AcoustID API Error: {err_msg}")
    except urllib.error.URLError as ue:
        logger.error(f"Failed to connect to AcoustID server: {ue.reason}")
        raise ValueError(f"Failed to connect to AcoustID server: {ue.reason}")
    except Exception as e:
        logger.error(f"Error calling AcoustID API: {e}")
        raise ValueError(f"Error calling AcoustID API: {e}")
        
    if res.get("status") != "ok":
        err_msg = res.get("error", {}).get("message", "Unknown error")
        logger.error(f"AcoustID lookup failed with status: {res.get('status')} - {err_msg}")
        raise ValueError(f"AcoustID API Error: {err_msg}")
        
    results = res.get("results", [])
    candidates = []
    
    for result in results:
        score = result.get("score", 0.0)
        recordings = result.get("recordings", [])
        for recording in recordings:
            title = recording.get("title") or "Unknown Title"
            artists = recording.get("artists", [])
            artist_name = ", ".join([a.get("name", "") for a in artists if a.get("name")]) if artists else "Unknown Artist"
            
            # Get release details
            release_mbid = None
            album_title = None
            releases = recording.get("releases", [])
            if releases:
                for r in releases:
                    if r.get("id"):
                        release_mbid = r.get("id")
                        album_title = r.get("title")
                        break
            
            candidates.append({
                "title": title,
                "artist": artist_name,
                "album": album_title,
                "release_mbid": release_mbid,
                "score": score
            })
            
    # Sort candidates by score descending
    candidates.sort(key=lambda x: x["score"], reverse=True)
    
    # Deduplicate candidates by artist and title (case insensitive)
    seen = set()
    deduped_candidates = []
    for c in candidates:
        artist_val = (c["artist"] or "").lower().strip()
        title_val = (c["title"] or "").lower().strip()
        key = (artist_val, title_val)
        if key not in seen:
            seen.add(key)
            deduped_candidates.append(c)
            
    return deduped_candidates

def apply_metadata_to_file(filepath: str, artist: str, title: str, album: Optional[str] = None, release_mbid: Optional[str] = None) -> str:
    """Applies metadata tags, downloads cover art, renames the file, and stores naming history."""
    path = Path(filepath)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {filepath}")
        
    artist = artist or "Unknown Artist"
    title = title or "Unknown Title"
        
    cover_bytes = None
    if release_mbid:
        raw_cover = fetch_cover_art(release_mbid)
        if raw_cover:
            cover_bytes = resize_image(raw_cover)
            
    # Write tags
    write_tags(filepath, title, artist, album, cover_bytes)
    
    # Determine new filename
    ext = path.suffix.lower()
    out_dir = path.parent
    clean_artist = sanitize_filename(artist)
    clean_title = sanitize_filename(title)
    
    new_stem = f"{clean_artist} - {clean_title}"
    new_path = out_dir / f"{new_stem}{ext}"
    
    if new_path != path:
        if new_path.exists():
            counter = 1
            while True:
                candidate = f"{new_stem}-{counter:02d}{ext}"
                new_path = out_dir / candidate
                if not new_path.exists():
                    break
                counter += 1
                
        # Store in naming history BEFORE renaming so we know the source and dest filenames
        history = load_naming_history(out_dir)
        
        original_name = path.name
        # If the file we are renaming was already identified previously, carry over its original filename
        if original_name in history:
            original_name = history[original_name]["original_name"]
            
        new_name = new_path.name
        
        # Check if WAV master exists and needs renaming
        wav_master = path.with_suffix(".wav")
        has_wav_master = wav_master.exists() and wav_master != path
        original_wav_name = wav_master.name if has_wav_master else None
        
        # If WAV master was already identified, carry over its original filename
        if has_wav_master and original_wav_name in history:
            original_wav_name = history[original_wav_name]["original_name"]
            
        new_wav_name = new_path.with_suffix(".wav").name if has_wav_master else None
        
        history[new_name] = {
            "original_name": original_name,
            "artist": artist,
            "title": title,
            "album": album,
            "release_mbid": release_mbid
        }
        
        if has_wav_master:
            history[new_wav_name] = {
                "original_name": original_wav_name,
                "artist": artist,
                "title": title,
                "album": album,
                "release_mbid": release_mbid
            }
            
        save_naming_history(out_dir, history)
        
        try:
            path.rename(new_path)
            logger.info(f"Renamed file: {path.name} -> {new_path.name}")
            
            if has_wav_master:
                new_wav_path = new_path.with_suffix(".wav")
                try:
                    write_tags(str(wav_master), title, artist, album, cover_bytes)
                    wav_master.rename(new_wav_path)
                    logger.info(f"Renamed matching WAV master: {wav_master.name} -> {new_wav_path.name}")
                except Exception as ex:
                    logger.error(f"Failed to rename associated WAV master: {ex}")
        except Exception as rename_err:
            logger.error(f"Failed to rename file to {new_path.name}: {rename_err}")
            # Revert history entry on failure
            history.pop(new_name, None)
            if has_wav_master:
                history.pop(new_wav_name, None)
            save_naming_history(out_dir, history)
            raise rename_err
            
    return str(new_path)

def remove_tags(filepath: str):
    """Removes all custom tags written by AcoustID (artist, title, album, cover art)."""
    ext = Path(filepath).suffix.lower()
    try:
        if ext == ".mp3":
            from mutagen.mp3 import MP3
            audio = MP3(filepath)
            audio.delete()
            audio.save()
            logger.info(f"Removed tags from MP3: {filepath}")
        elif ext == ".flac":
            from mutagen.flac import FLAC
            audio = FLAC(filepath)
            audio.delete()
            audio.save()
            logger.info(f"Removed tags from FLAC: {filepath}")
        elif ext in [".m4a", ".mp4"]:
            from mutagen.mp4 import MP4
            audio = MP4(filepath)
            audio.delete()
            audio.save()
            logger.info(f"Removed tags from M4A: {filepath}")
        elif ext == ".wav":
            from mutagen.wave import WAVE
            audio = WAVE(filepath)
            audio.delete()
            audio.save()
            logger.info(f"Removed tags from WAV: {filepath}")
    except Exception as e:
        logger.error(f"Failed to remove tags from {filepath}: {e}")

def undo_identify_file(filepath: str) -> str:
    """Reverts a file to its original filename and strips its tags."""
    path = Path(filepath)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {filepath}")
        
    out_dir = path.parent
    history = load_naming_history(out_dir)
    
    filename = path.name
    if filename not in history:
        raise ValueError(f"No naming history found for: {filename}")
        
    entry = history[filename]
    orig_name = entry["original_name"]
    orig_path = out_dir / orig_name
    
    # Check if WAV master or transcoded pair exists in history as well
    paired_filename = None
    paired_orig_name = None
    
    orig_stem = Path(orig_name).stem
    for k, v in history.items():
        if k != filename and Path(v["original_name"]).stem == orig_stem:
            paired_filename = k
            paired_orig_name = v["original_name"]
            break
            
    # Revert main file
    if orig_path.exists():
        raise FileExistsError(f"Original file already exists: {orig_path.name}")
        
    # Remove tags first
    remove_tags(str(path))
    
    # Rename back
    path.rename(orig_path)
    logger.info(f"Undid rename: {filename} -> {orig_name}")
    
    # Revert paired file (e.g. WAV master) if it exists
    if paired_filename:
        paired_path = out_dir / paired_filename
        paired_orig_path = out_dir / paired_orig_name
        if paired_path.exists() and not paired_orig_path.exists():
            remove_tags(str(paired_path))
            paired_path.rename(paired_orig_path)
            logger.info(f"Undid paired rename: {paired_filename} -> {paired_orig_name}")
            
    # Clean history
    history.pop(filename, None)
    if paired_filename:
        history.pop(paired_filename, None)
    save_naming_history(out_dir, history)
    
    return str(orig_path)

def identify_file(filepath: str, api_key: str, confidence_threshold: float) -> Optional[dict]:
    """Orchestrates fingerprinting, querying, metadata embedding, and renaming."""
    path = Path(filepath)
    if not path.exists():
        logger.error(f"File for identification does not exist: {filepath}")
        return None
        
    try:
        logger.info(f"Fingerprinting file: {filepath}")
        duration, fingerprint = get_fingerprint(filepath)
    except Exception as e:
        logger.error(f"Failed to generate fingerprint: {e}")
        return None
        
    logger.info(f"Looking up fingerprint on AcoustID (duration={duration}s)...")
    match = lookup_acoustid(duration, fingerprint, api_key, confidence_threshold)
    if not match:
        logger.info(f"No match found for file: {filepath}")
        return None
        
    artist = match["artist"]
    title = match["title"]
    album = match["album"]
    mbid = match["release_mbid"]
    
    logger.info(f"AcoustID matched: {artist} - {title} (MBID: {mbid})")
    
    try:
        new_filepath = apply_metadata_to_file(filepath, artist, title, album, mbid)
    except Exception as err:
        logger.error(f"Failed to apply metadata during auto-identify: {err}")
        return None
        
    return {
        "artist": artist,
        "title": title,
        "album": album,
        "new_path": new_filepath
    }
