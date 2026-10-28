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



def get_video_dimensions(video_path: str) -> Tuple[int, int]:
    """Returns (width, height) of the video using ffprobe."""
    try:
        result = subprocess.run(
            [
                FFPROBE_BIN,
                "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=width,height",
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
        streams = data.get("streams", [])
        if streams:
            w = int(streams[0].get("width", 1920))
            h = int(streams[0].get("height", 1080))
            return max(16, w), max(16, h)
    except Exception:
        pass
    return 1920, 1080

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


def build_fit_filter_tail(
    subtitle_name: Optional[str] = None,
    target_width: int = TARGET_WIDTH,
    target_height: int = TARGET_HEIGHT,
) -> str:
    """Builds a filter-graph tail that fits the FULL frame inside the target
    canvas, filling the leftover top/bottom space with a blurred, darkened
    copy of the frame (classic Shorts/Reels pillarbox style).

    Used when no face is present (e.g. first-person gameplay): center-cropping
    would cut HUD/inventory elements on the sides, so the whole frame is kept
    visible instead. ``{IN}`` inside the template is replaced with the input
    label ("0:v" or a filter_complex label such as "vc").
    """
    subtitle_part = f",subtitles={subtitle_name}" if subtitle_name else ""
    return (
        "{IN}split=2[fitbg][fitfg];"
        f"[fitbg]scale={target_width}:{target_height}:force_original_aspect_ratio=increase,"
        f"crop={target_width}:{target_height},"
        "boxblur=24:2,"
        "eq=brightness=-0.08[fitbgd];"
        f"[fitfg]scale={target_width}:{target_height}:force_original_aspect_ratio=decrease[fitfgd];"
        f"[fitbgd][fitfgd]overlay=(W-w)/2:(H-h)/2,setsar=1{subtitle_part}[vout]"
    )


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
    fit_tail: Optional[str] = None,
) -> Tuple[str, str, str]:
    """Returns (filter_complex, video_label, audio_label).

    When ``fit_tail`` is provided (full-frame blurred-pillarbox mode) it
    replaces the linear video filter and receives the concat output label
    via the ``{IN}`` placeholder.
    """
    chain_parts: List[str] = []
    for index, (start, end) in enumerate(intervals):
        chain_parts.append(
            f"[0:v]trim=start={start:.3f}:end={end:.3f},setpts=PTS-STARTPTS[v{index}]"
        )
        if has_audio:
            chain_parts.append(
                f"[0:a]atrim=start={start:.3f}:end={end:.3f},asetpts=PTS-STARTPTS[a{index}]"
            )

    if len(intervals) == 1:
        video_in = "[v0]"
        audio_out = "[a0]" if has_audio else ""
    else:
        if has_audio:
            inputs = "".join(f"[v{i}][a{i}]" for i in range(len(intervals)))
            chain_parts.append(f"{inputs}concat=n={len(intervals)}:v=1:a=1[vc][ac]")
            video_in = "[vc]"
            audio_out = "[ac]"
        else:
            inputs = "".join(f"[v{i}]" for i in range(len(intervals)))
            chain_parts.append(f"{inputs}concat=n={len(intervals)}:v=1:a=0[vc]")
            video_in = "[vc]"
            audio_out = ""

    if fit_tail:
        chain_parts.append(fit_tail.replace("{IN}", video_in))
    else:
        chain_parts.append(f"{video_in}{video_filter}[vout]")
    return ";".join(chain_parts), "[vout]", audio_out


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
    fit_full_frame: bool = False,
) -> Dict[str, Any]:
    """
    Cuts a clip from ``video_path``, reframes it to the requested aspect ratio
    and burns the ASS subtitles. ``keep_intervals`` (absolute source timestamps)
    enables auto jump-cuts by removing long pauses.

    When ``fit_full_frame`` is True the whole source frame is kept visible on
    the target canvas with a blurred background instead of being cropped
    (used when no face is detected, e.g. first-person gameplay).

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

    fit_tail = (
        build_fit_filter_tail(subtitle_name, target_width, target_height)
        if fit_full_frame
        else None
    )
    video_filter = (
        None if fit_full_frame
        else build_video_filters(crop, subtitle_name, target_width, target_height)
    )
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
                video_filter or "", intervals, has_audio, fit_tail=fit_tail
            )
            cmd += ["-filter_complex", filter_complex, "-map", video_label]
            if has_audio and audio_label:
                cmd += ["-map", audio_label]
            else:
                cmd += ["-an"]
        elif fit_tail:
            # No cuts: apply the fit graph directly on the input stream.
            cmd += ["-filter_complex", fit_tail.replace("{IN}", "[0:v]"), "-map", "[vout]"]
            if has_audio:
                cmd += ["-map", "0:a:0"]
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


def render_full_video(
    video_path: str,
    output_path: str,
    subtitle_path: Optional[str] = None,
    sticker_events: Optional[List[Dict[str, Any]]] = None,
    use_nvenc: bool = True,
    progress_cb: ProgressCallback = None,
    cancel_event: Optional[threading.Event] = None,
    target_aspect: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Renders the full-length video, burning animated subtitles and
    overlaying timeline sticker badges (Subscribe, Like, Comment, Share).
    Keeps original resolution by default.
    """
    from . import sticker_generator

    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    duration = get_video_duration(video_path)
    if duration <= 0:
        duration = 10.0

    orig_w, orig_h = get_video_dimensions(video_path)
    orig_w = orig_w - (orig_w % 2)
    orig_h = orig_h - (orig_h % 2)

    if target_aspect and target_aspect in ["9:16", "1:1", "16:9"] and target_aspect != "original":
        from .config import ASPECT_RATIOS
        target_w, target_h = ASPECT_RATIOS[target_aspect]
    else:
        target_w, target_h = orig_w, orig_h

    target_w = target_w - (target_w % 2)
    target_h = target_h - (target_h % 2)

    cwd = None
    sub_filter_str = None
    if subtitle_path:
        sub_file = Path(subtitle_path).resolve()
        sub_filter_str = f"subtitles={sub_file.name}"
        cwd = str(sub_file.parent)

    has_audio = has_audio_stream(video_path)

    # 1. Base video filter
    vf_base_parts = []
    if (target_w, target_h) != (orig_w, orig_h):
        vf_base_parts.append(f"scale={target_w}:{target_h}:force_original_aspect_ratio=increase")
        vf_base_parts.append(f"crop={target_w}:{target_h}")
        vf_base_parts.append("setsar=1")
    else:
        vf_base_parts.append(f"scale={target_w}:{target_h}")
        vf_base_parts.append("setsar=1")

    # 2. Build full filter chain
    extra_input_args: List[str] = []
    fc_parts: List[str] = []

    if vf_base_parts:
        fc_parts.append(f"[0:v]{','.join(vf_base_parts)}[vscaled]")
        current_v = "[vscaled]"
    else:
        current_v = "[0:v]"

    if sub_filter_str:
        fc_parts.append(f"{current_v}{sub_filter_str}[vsub]")
        current_v = "[vsub]"

    if sticker_events:
        stk_inputs, stk_filters, current_v = sticker_generator.build_sticker_overlay_filter(
            sticker_events,
            video_width=target_w,
            video_height=target_h,
            start_input_index=1,
            base_video_label=current_v,
        )
        extra_input_args.extend(stk_inputs)
        fc_parts.extend(stk_filters)

    fc_parts.append(f"{current_v}null[vout]")
    filter_complex = ";".join(fc_parts)

    def build_cmd(codec: str) -> list:
        cmd = [
            FFMPEG_BIN,
            "-y",
            "-hide_banner",
            "-loglevel", "error",
            "-i", str(Path(video_path).resolve()),
        ]
        cmd += extra_input_args
        cmd += [
            "-filter_complex", filter_complex,
            "-map", "[vout]",
        ]
        if has_audio:
            cmd += ["-map", "0:a:0?"]
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
            progress_cb(0.0, f"Renderizando vídeo completo com {label}...")
        return _run_ffmpeg(
            build_cmd(codec),
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
            print(f"[!] NVENC falhou na renderização completa, usando libx264: {log[-500:]}")
            output.unlink(missing_ok=True)
            returncode, log = attempt("libx264")
    else:
        returncode, log = attempt("libx264")

    if returncode != 0 or not output.exists():
        msg = f"Falha ao renderizar video: {log[-1200:]}"
        raise RuntimeError(msg)

    if progress_cb:
        progress_cb(100.0, "Vídeo completo renderizado com sucesso!")

    return {
        "path": str(output.resolve()),
        "filename": output.name,
        "encoder": encoder_used,
        "duration": round(duration, 2),
        "width": target_w,
        "height": target_h,
        "stickers_count": len(sticker_events) if sticker_events else 0,
        "size_bytes": output.stat().st_size,
    }