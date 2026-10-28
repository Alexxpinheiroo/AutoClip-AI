import json
import os
import subprocess
import threading
from pathlib import Path
from typing import Callable, Dict, Any, List, Optional, Tuple

from .config import (
    AUDIO_BITRATE,
    FFMPEG_BIN,
    FFPROBE_BIN,
    TARGET_HEIGHT,
    TARGET_WIDTH,
    VIDEO_CRF,
)

ProgressCallback = Optional[Callable[[float, str], None]]


class RenderCancelled(Exception):
    """Raised when the user cancels a running ffmpeg render."""


def _creation_flags() -> int:
    if os.name == "nt":
        return getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return 0


def get_video_duration(video_path: str) -> float:
    """Returns the duration of a video in seconds using ffprobe."""
    try:
        result = subprocess.run(
            [
                FFPROBE_BIN,
                "-v", "error",
                "-show_entries", "format=duration",
                "-of", "json",
                video_path,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=_creation_flags(),
        )
        data = json.loads(result.stdout or "{}")
        return float(data.get("format", {}).get("duration", 0.0) or 0.0)
    except Exception:
        return 0.0


def has_audio_stream(video_path: str) -> bool:
    """Returns True when the file has at least one audio stream."""
    try:
        result = subprocess.run(
            [
                FFPROBE_BIN,
                "-v", "error",
                "-select_streams", "a:0",
                "-show_entries", "stream=index",
                "-of", "csv=p=0",
                video_path,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=_creation_flags(),
        )
        return bool((result.stdout or "").strip())
    except Exception:
        return True


def _run_ffmpeg(
    cmd: list,
    duration: float,
    progress_cb: ProgressCallback = None,
    cancel_event: Optional[threading.Event] = None,
    cwd: Optional[str] = None,
) -> Tuple[int, str]:
    """Runs ffmpeg, streaming ``-progress`` output and detecting cancellation."""
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=cwd,
        creationflags=_creation_flags(),
    )

    output_lines = []
    try:
        assert process.stdout is not None
        for line in process.stdout:
            line = line.strip()
            if not line:
                continue
            output_lines.append(line)
            if len(output_lines) > 80:
                output_lines.pop(0)

            if cancel_event is not None and cancel_event.is_set():
                process.kill()
                raise RenderCancelled("Renderização cancelada pelo usuário.")

            if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
                raw = line.split("=", 1)[1]
                if raw.isdigit() and duration > 0:
                    seconds = int(raw) / 1_000_000.0
                    percent = max(0.0, min(100.0, (seconds / duration) * 100.0))
                    if progress_cb:
                        progress_cb(percent, f"Renderizando vídeo... {percent:.0f}%")
    finally:
        process.wait()
        if process.stdout is not None:
            process.stdout.close()

    return process.returncode, "\n".join(output_lines)


def build_video_filters(
    crop: Optional[Tuple[int, int, int, int]],
    subtitle_name: Optional[str] = None,
    target_width: int = TARGET_WIDTH,
    target_height: int = TARGET_HEIGHT,
) -> str:
    """Builds the ffmpeg video chain: face crop + aspect scale + captions."""
    filters = []
    if crop:
        crop_w, crop_h, crop_x, crop_y = crop
        filters.append(f"crop={crop_w}:{crop_h}:{crop_x}:{crop_y}")
    filters.append(
        f"scale={target_width}:{target_height}:force_original_aspect_ratio=increase"
    )
    filters.append(f"crop={target_width}:{target_height}")
    filters.append("setsar=1")
    if subtitle_name:
        # The renderer runs with cwd set to the subtitle folder, which avoids
        # Windows drive-letter escaping problems in ffmpeg filter paths.
        filters.append(f"subtitles={subtitle_name}")
    return ",".join(filters)


def _normalize_keep_intervals(
    keep_intervals: Optional[List[Tuple[float, float]]],
    start_time: float,
    end_time: float,
) -> Optional[List[Tuple[float, float]]]:
    """Converts absolute source intervals to clip-relative, merged intervals."""
    if not keep_intervals:
        return None

    relative: List[Tuple[float, float]] = []
    for raw_start, raw_end in keep_intervals:
        a = max(start_time, float(raw_start)) - start_time
        b = min(end_time, float(raw_end)) - start_time
        if b - a < 0.06:
            continue
        if relative and a - relative[-1][1] <= 0.04:
            relative[-1] = (relative[-1][0], max(relative[-1][1], b))
        else:
            relative.append((a, b))

    if not relative:
        return None
    total = sum(b - a for a, b in relative)
    full = max(0.1, end_time - start_time)
    if total < 1.5 or total >= full * 0.992:
        return None
    return relative


def _build_filter_complex(
    video_filter: str,
    intervals: List[Tuple[float, float]],
    has_audio: bool,
) -> Tuple[str, str, str]:
    """Returns (filter_complex, video_label, audio_label)."""
    parts: List[str] = []
    for index, (start, end) in enumerate(intervals):
        parts.append(
            f"[0:v]trim=start={start:.3f}:end={end:.3f},setpts=PTS-STARTPTS[v{index}]"
        )
        if has_audio:
            parts.append(
                f"[0:a]atrim=start={start:.3f}:end={end:.3f},asetpts=PTS-STARTPTS[a{index}]"
            )

    if len(intervals) == 1:
        video_in = "[v0]"
        audio_out = "[a0]" if has_audio else ""
    else:
        if has_audio:
            inputs = "".join(f"[v{i}][a{i}]" for i in range(len(intervals)))
            parts.append(f"{inputs}concat=n={len(intervals)}:v=1:a=1[vc][ac]")
            video_in = "[vc]"
            audio_out = "[ac]"
        else:
            inputs = "".join(f"[v{i}]" for i in range(len(intervals)))
            parts.append(f"{inputs}concat=n={len(intervals)}:v=1:a=0[vc]")
            video_in = "[vc]"
            audio_out = ""

    parts.append(f"{video_in}{video_filter}[vout]")
    return ";".join(parts), "[vout]", audio_out


def render_clip(
    video_path: str,
    output_path: str,
    start_time: float,
    end_time: float,
    crop: Optional[Tuple[int, int, int, int]] = None,
    subtitle_path: Optional[str] = None,
    use_nvenc: bool = True,
    progress_cb: ProgressCallback = None,
    cancel_event: Optional[threading.Event] = None,
    target_width: int = TARGET_WIDTH,
    target_height: int = TARGET_HEIGHT,
    keep_intervals: Optional[List[Tuple[float, float]]] = None,
    edited_duration: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Cuts a clip from ``video_path``, reframes it to the requested aspect ratio
    and burns the ASS subtitles. ``keep_intervals`` (absolute source timestamps)
    enables auto jump-cuts by removing long pauses.

    Uses NVENC when available and transparently falls back to libx264 if
    hardware encoding fails.
    """
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    full_duration = max(0.1, float(end_time) - float(start_time))
    intervals = _normalize_keep_intervals(keep_intervals, float(start_time), float(end_time))
    use_cuts = intervals is not None
    if use_cuts and edited_duration is None:
        edited_duration = sum(b - a for a, b in intervals)
    duration = max(0.1, float(edited_duration) if edited_duration is not None else full_duration)

    subtitle_name = None
    cwd = None
    if subtitle_path:
        subtitle = Path(subtitle_path).resolve()
        subtitle_name = subtitle.name
        cwd = str(subtitle.parent)

    video_filter = build_video_filters(crop, subtitle_name, target_width, target_height)
    has_audio = has_audio_stream(video_path)

    def build_command(codec: str) -> list:
        cmd = [
            FFMPEG_BIN,
            "-y",
            "-hide_banner",
            "-loglevel", "error",
            "-ss", f"{start_time:.3f}",
            "-t", f"{full_duration:.3f}",
            "-i", str(Path(video_path).resolve()),
        ]

        if use_cuts and intervals:
            filter_complex, video_label, audio_label = _build_filter_complex(
                video_filter, intervals, has_audio
            )
            cmd += ["-filter_complex", filter_complex, "-map", video_label]
            if has_audio and audio_label:
                cmd += ["-map", audio_label]
            else:
                cmd += ["-an"]
        else:
            cmd += ["-vf", video_filter, "-map", "0:v:0"]
            if has_audio:
                cmd += ["-map", "0:a:0"]
            else:
                cmd += ["-an"]

        cmd += ["-pix_fmt", "yuv420p"]
        if codec == "nvenc":
            cmd += [
                "-c:v", "h264_nvenc",
                "-preset", "p5",
                "-tune", "hq",
                "-rc", "vbr",
                "-cq", str(VIDEO_CRF),
                "-b:v", "0",
            ]
        else:
            cmd += [
                "-c:v", "libx264",
                "-preset", "medium",
                "-crf", str(VIDEO_CRF),
            ]
        if has_audio:
            cmd += [
                "-c:a", "aac",
                "-b:a", AUDIO_BITRATE,
                "-ar", "48000",
            ]
        cmd += [
            "-movflags", "+faststart",
            "-progress", "pipe:1",
            "-nostats",
            str(output),
        ]
        return cmd

    def attempt(codec: str) -> Tuple[int, str]:
        if progress_cb:
            label = "GPU (NVENC)" if codec == "nvenc" else "CPU (libx264)"
            progress_cb(0.0, f"Renderizando com {label}...")
        return _run_ffmpeg(
            build_command(codec),
            duration,
            progress_cb=progress_cb,
            cancel_event=cancel_event,
            cwd=cwd,
        )

    encoder_used = "libx264"
    if use_nvenc:
        returncode, log = attempt("nvenc")
        if returncode == 0 and output.exists() and output.stat().st_size > 0:
            encoder_used = "h264_nvenc"
        else:
            print(f"[!] NVENC falhou, usando libx264. Detalhes: {log[-600:]}")
            output.unlink(missing_ok=True)
            returncode, log = attempt("libx264")
    else:
        returncode, log = attempt("libx264")

    if returncode != 0 or not output.exists():
        raise RuntimeError(
            f"Falha ao renderizar o clipe com ffmpeg.\n{log[-1500:]}"
        )

    if progress_cb:
        progress_cb(100.0, "Clipe renderizado.")

    return {
        "path": str(output.resolve()),
        "filename": output.name,
        "encoder": encoder_used,
        "duration": round(duration, 2),
        "auto_edited": bool(use_cuts),
        "kept_intervals": len(intervals) if intervals else 1,
        "size_bytes": output.stat().st_size,
    }


def extract_thumbnail(
    video_path: str,
    output_path: str,
    at_seconds: float = 0.5,
) -> Optional[str]:
    """Extracts a JPEG thumbnail from a rendered clip."""
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        FFMPEG_BIN,
        "-y",
        "-hide_banner",
        "-loglevel", "error",
        "-ss", f"{max(0.0, at_seconds):.2f}",
        "-i", str(Path(video_path).resolve()),
        "-frames:v", "1",
        "-q:v", "3",
        str(output),
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=_creation_flags(),
        )
        if result.returncode == 0 and output.exists():
            return str(output.resolve())
    except Exception as exc:
        print(f"[!] Não foi possível gerar thumbnail: {exc}")
    return None
