import os
import re
import uuid
from pathlib import Path
from typing import Callable, Optional, Dict, Any
import yt_dlp
from .config import DOWNLOADS_DIR

def sanitize_filename(name: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "", name).strip()

def download_video(
    url: str, 
    progress_callback: Optional[Callable[[float, str], None]] = None
) -> Dict[str, Any]:
    """
    Downloads video from YouTube or other supported platforms using yt-dlp.
    Returns metadata dict with local file path, title, duration.
    """
    video_id = str(uuid.uuid4())[:8]
    output_template = str(DOWNLOADS_DIR / f"{video_id}_%(title).50s.%(ext)s")

    def yt_progress_hook(d):
        if progress_callback and d.get('status') == 'downloading':
            total = d.get('total_bytes') or d.get('total_bytes_estimate') or 1
            downloaded = d.get('downloaded_bytes', 0)
            percent = (downloaded / total) * 100
            speed = d.get('_speed_str', '')
            eta = d.get('_eta_str', '')
            progress_callback(percent, f"Baixando vídeo... {percent:.1f}% ({speed}, restante: {eta})")

    ydl_opts = {
        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best',
        'outtmpl': output_template,
        'progress_hooks': [yt_progress_hook],
        'quiet': True,
        'no_warnings': True,
        'merge_output_format': 'mp4',
        'postprocessors': [{
            'key': 'FFmpegVideoConvertor',
            'preferedformat': 'mp4',
        }],
    }

    if progress_callback:
        progress_callback(5.0, "Obtendo informações do vídeo...")

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        expected_filename = ydl.prepare_filename(info)

        # yt-dlp may change the extension after merging/converting.
        # Search for the actual file produced with the same base name.
        base = os.path.splitext(expected_filename)[0]
        actual_filename = None
        for ext in (".mp4", ".mkv", ".webm", ".mov", ".avi"):
            candidate = f"{base}{ext}"
            if os.path.exists(candidate):
                actual_filename = candidate
                break

        if actual_filename is None:
            # Last resort: look for any file in DOWNLOADS_DIR starting with video_id
            for f in DOWNLOADS_DIR.iterdir():
                if f.name.startswith(video_id) and f.is_file():
                    actual_filename = str(f)
                    break

        if actual_filename is None:
            raise FileNotFoundError(
                f"Não foi possível localizar o arquivo baixado para: {url}"
            )

        duration = info.get('duration', 0)
        title = info.get('title', 'Vídeo sem título')
        thumbnail = info.get('thumbnail', '')

    return {
        "video_id": video_id,
        "filepath": str(Path(actual_filename).resolve()),
        "title": title,
        "duration": duration,
        "thumbnail": thumbnail
    }

def save_uploaded_video(
    source_bytes: bytes, 
    original_filename: str
) -> Dict[str, Any]:
    """
    Saves a locally uploaded video file into the downloads folder.
    """
    video_id = str(uuid.uuid4())[:8]
    clean_name = sanitize_filename(original_filename)
    dest_path = DOWNLOADS_DIR / f"{video_id}_{clean_name}"
    
    with open(dest_path, "wb") as f:
        f.write(source_bytes)
        
    # Get duration via ffprobe (more reliable than cv2)
    from .video_renderer import get_video_duration
    duration = get_video_duration(str(dest_path))

    return {
        "video_id": video_id,
        "filepath": str(dest_path.resolve()),
        "title": original_filename,
        "duration": round(duration, 2),
        "thumbnail": ""
    }
