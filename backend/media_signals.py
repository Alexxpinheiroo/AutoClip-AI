"""Lightweight multimodal signals extracted locally with ffmpeg.

The goal is not to understand *what* is on screen (that would require a large
vision model), but to give the clip selector objective evidence that a moment
is lively: loud/excited audio, rapid scene changes, and little dead air.

Only ffmpeg + numpy are required, so this works on any machine that can already
render clips.
"""
from __future__ import annotations

import os
import re
import subprocess
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from .config import FFMPEG_BIN

ANALYSIS_WINDOW = 0.5  # seconds per audio window
SAMPLE_RATE = 8000


def _creation_flags() -> int:
    if os.name == "nt":
        return getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return 0


def _run(cmd: List[str], timeout: float = 600.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        capture_output=True,
        text=False,
        timeout=timeout,
        creationflags=_creation_flags(),
    )


def analyze_audio(video_path: str, timeout: float = 600.0) -> Optional[Dict[str, Any]]:
    """Decodes mono PCM audio and returns per-window RMS/energy values."""
    cmd = [
        FFMPEG_BIN,
        "-hide_banner",
        "-loglevel", "error",
        "-nostdin",
        "-i", str(video_path),
        "-vn",
        "-ac", "1",
        "-ar", str(SAMPLE_RATE),
        "-f", "s16le",
        "-",
    ]
    try:
        result = _run(cmd, timeout=timeout)
        if result.returncode != 0 or not result.stdout:
            return None
        samples = np.frombuffer(result.stdout, dtype=np.int16).astype(np.float32) / 32768.0
    except Exception as exc:
        print(f"[!] Análise de áudio indisponível: {exc}")
        return None

    if samples.size == 0:
        return None

    window_samples = max(1, int(SAMPLE_RATE * ANALYSIS_WINDOW))
    usable = (samples.size // window_samples) * window_samples
    if usable == 0:
        return None
    frames = samples[:usable].reshape(-1, window_samples)

    rms = np.sqrt(np.mean(np.square(frames), axis=1) + 1e-12)
    db = 20.0 * np.log10(rms + 1e-8)

    # Normalize against the speech part of THIS video, so a quiet recording is
    # not punished simply because it was recorded at a lower gain.
    speech = db[db > -55.0]
    if speech.size >= 8:
        low = float(np.percentile(speech, 15))
        high = float(np.percentile(speech, 90))
    else:
        low, high = -45.0, -12.0
    if high - low < 6.0:
        low, high = -42.0, -12.0

    energy = np.clip((db - low) / max(1.0, high - low), 0.0, 1.0)
    duration = samples.size / float(SAMPLE_RATE)

    return {
        "window_seconds": ANALYSIS_WINDOW,
        "duration": round(duration, 2),
        "db": [round(float(v), 1) for v in db.tolist()],
        "energy": [round(float(v), 3) for v in energy.tolist()],
    }


def detect_scene_cuts(video_path: str, timeout: float = 900.0, fps: float = 2.0) -> List[float]:
    """Returns approximate timestamps (seconds) where the visual scene changes."""
    cmd = [
        FFMPEG_BIN,
        "-hide_banner",
        "-loglevel", "info",
        "-nostdin",
        "-i", str(video_path),
        "-an",
        "-vf", f"fps={fps},scale=320:-2,select='gt(scene,0.28)',showinfo",
        "-f", "null",
        "-",
    ]
    try:
        result = _run(cmd, timeout=timeout)
    except Exception as exc:
        print(f"[!] Detecção de cenas indisponível: {exc}")
        return []

    stderr = (result.stderr or b"").decode("utf-8", errors="replace")
    times: List[float] = []
    for match in re.finditer(r"pts_time:([0-9]+(?:\.[0-9]+)?)", stderr):
        try:
            value = float(match.group(1))
        except ValueError:
            continue
        # ``fps`` keeps source timestamps, but guard against duplicates.
        if not times or value - times[-1] > 0.15:
            times.append(round(value, 3))
    return times


def analyze(video_path: str, total_duration: float = 0.0) -> Dict[str, Any]:
    """Runs the available signal extractors and returns everything needed by
    :mod:`backend.analyzer`."""
    audio = analyze_audio(video_path)
    scenes = detect_scene_cuts(video_path)
    return {
        "audio": audio,
        "scene_cuts": scenes,
        "duration": round(float(total_duration or (audio or {}).get("duration", 0.0)), 2),
    }


def _slice_audio(audio: Optional[Dict[str, Any]], start: float, end: float) -> Optional[Dict[str, float]]:
    if not audio:
        return None
    window = float(audio.get("window_seconds") or ANALYSIS_WINDOW)
    energy = audio.get("energy") or []
    db = audio.get("db") or []
    if not energy:
        return None
    first = max(0, int(start / window))
    last = min(len(energy), max(first + 1, int(np.ceil(end / window))))
    values = np.asarray(energy[first:last], dtype=np.float32)
    db_values = np.asarray(db[first:last], dtype=np.float32)
    if values.size == 0:
        return None
    return {
        "mean": float(np.mean(values)),
        "peak": float(np.max(values)),
        "high_ratio": float(np.mean(values >= 0.65)),
        "silence_ratio": float(np.mean(values <= 0.18)),
        "variance": float(np.var(values)),
        "mean_db": float(np.mean(db_values)),
    }


def summarize_window(
    signals: Optional[Dict[str, Any]],
    start: float,
    end: float,
) -> Dict[str, Any]:
    """Condenses raw signals into the 0-100 features used by the scorer."""
    duration = max(0.1, float(end) - float(start))
    if not signals:
        return {
            "audio_score": 55,
            "visual_score": 55,
            "emotion_score": 50,
            "silence_ratio": 0.0,
            "scene_cuts": 0,
            "cuts_per_minute": 0.0,
            "audio_mean": 0.0,
            "audio_peak": 0.0,
            "available": False,
        }

    audio = _slice_audio(signals.get("audio"), start, end)
    scenes = signals.get("scene_cuts") or []
    cuts = sum(1 for t in scenes if start <= float(t) <= end)
    cuts_per_minute = cuts / duration * 60.0

    if audio:
        # Loudness + sustained energy + dynamic peaks. Speech-only podcasts
        # tend to land around 55-70, while laughter/shouting pushes 80+.
        audio_score = (
            18.0
            + audio["mean"] * 42.0
            + audio["peak"] * 20.0
            + audio["high_ratio"] * 20.0
            + min(12.0, audio["variance"] * 55.0)
        )
        emotion_score = (
            30.0
            + audio["variance"] * 130.0
            + audio["high_ratio"] * 35.0
            + (audio["peak"] - audio["mean"]) * 40.0
        )
        silence_ratio = audio["silence_ratio"]
    else:
        audio_score = 55.0
        emotion_score = 50.0
        silence_ratio = 0.0

    # A talking-head with zero cuts is not "bad", just visually static, so the
    # baseline stays around 50 instead of collapsing to zero.
    visual_score = 48.0 + min(42.0, cuts_per_minute * 9.0)

    # Fast cuts combined with loud audio usually means action/excitement.
    if cuts_per_minute >= 8 and audio and audio["mean"] >= 0.55:
        emotion_score += 8.0

    return {
        "audio_score": int(round(max(0.0, min(100.0, audio_score)))),
        "visual_score": int(round(max(0.0, min(100.0, visual_score)))),
        "emotion_score": int(round(max(0.0, min(100.0, emotion_score)))),
        "silence_ratio": round(float(silence_ratio), 3),
        "scene_cuts": int(cuts),
        "cuts_per_minute": round(float(cuts_per_minute), 1),
        "audio_mean": round(float((audio or {}).get("mean", 0.0)), 3),
        "audio_peak": round(float((audio or {}).get("peak", 0.0)), 3),
        "available": True,
    }
