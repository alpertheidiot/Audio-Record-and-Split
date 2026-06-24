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
            logger.error(f"AcoustID lookup failed with status: {res.get('status')}")
            return None
            
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
    except Exception as e:
        logger.error(f"Error calling AcoustID API: {e}")
        return None

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
    """Crop and resize the cover image to square dimensions between 600x600 and 1000x1000."""
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
            
        # Target sizing range: [600, 1000]
        size = img.width
        if size < 600:
            size = 600
        elif size > 1000:
            size = 1000
            
        if size != img.width:
            logger.info(f"Resizing cover art from {img.width}x{img.height} to {size}x{size}")
            img = img.resize((size, size), Image.Resampling.LANCZOS)
            
        out_io = io.BytesIO()
        img.save(out_io, format="JPEG", quality=85)
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
    
    cover_bytes = None
    if mbid:
        raw_cover = fetch_cover_art(mbid)
        if raw_cover:
            cover_bytes = resize_image(raw_cover)
            
    # Write tags
    write_tags(filepath, title, artist, album, cover_bytes)
    
    # Rename file
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
        
        try:
            path.rename(new_path)
            logger.info(f"Renamed file: {path.name} -> {new_path.name}")
            
            # If there is a matching WAV master, rename it too
            wav_master = path.with_suffix(".wav")
            if wav_master.exists() and wav_master != path:
                new_wav_path = new_path.with_suffix(".wav")
                try:
                    wav_master.rename(new_wav_path)
                    logger.info(f"Renamed matching WAV master: {wav_master.name} -> {new_wav_path.name}")
                except Exception as ex:
                    logger.error(f"Failed to rename associated WAV master: {ex}")
        except Exception as rename_err:
            logger.error(f"Failed to rename file to {new_path.name}: {rename_err}")
            new_path = path
            
    return {
        "artist": artist,
        "title": title,
        "album": album,
        "new_path": str(new_path)
    }
