# -*- coding: utf-8 -*-
"""
Chapter Generator — AutoClip-AI

Detects chapter boundaries automatically from Whisper transcription segments
using a multi-signal approach:

  1. Silence gaps: Pauses longer than a threshold signal topic transitions.
  2. Sentence density: Low-word-rate regions suggest topic shifts.
  3. Sentence endings: Segments ending with full-stop punctuation often
     mark topical closures.
  4. Minimum chapter length: Prevents micro-chapters.
  5. Title extraction: Derives a concise title from the first meaningful
     words of each chapter segment.

Output: a list of chapter dicts  ->  {"start": float, "title": str}
Plus  : YouTube-ready timestamp text  ->  "0:00 Introducao\n2:14 Tema X\n..."
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional


def _fmt_yt(seconds: float) -> str:
    """Formats seconds as M:SS or H:MM:SS for YouTube chapter timestamps."""
    s = max(0, int(round(seconds)))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{sec:02d}"
    return f"{m}:{sec:02d}"


def _clean_title(text: str, max_words: int = 6, max_chars: int = 50) -> str:
    """Derives a short, clean chapter title from a raw segment text."""
    text = re.sub(r"^\s*[,;:\-\u2013\u2014\.…]+\s*", "", text).strip()
    text = re.sub(r"\s+", " ", text)
    fillers = re.compile(
        r"^(ent\u00e3o|e\s|a\u00ed\s|n\u00e9\s|mas\s|ok\s|certo\s|bom\s|olha\s|"
        r"so\s|and\s|but\s|right\s|well\s|now\s|alright\s)+",
        re.IGNORECASE,
    )
    text = fillers.sub("", text).strip()
    if text:
        text = text[0].upper() + text[1:]
    words = text.split()
    if len(words) > max_words:
        text = " ".join(words[:max_words]).rstrip(".,;:!?") + "..."
    if len(text) > max_chars:
        text = text[: max_chars - 3].rstrip() + "..."
    return text or "Cap\u00edtulo"


def _word_rate(segments: List[Dict[str, Any]], start: float, end: float) -> float:
    """Words-per-second in the [start, end) window."""
    total_words = 0
    duration = max(1.0, end - start)
    for seg in segments:
        if seg["end"] <= start or seg["start"] >= end:
            continue
        total_words += len(str(seg.get("text", "")).split())
    return total_words / duration


def detect_chapters(
    segments: List[Dict[str, Any]],
    total_duration: float,
    min_chapter_duration: float = 60.0,
    max_chapters: int = 20,
    silence_threshold: float = 2.0,
    low_word_rate_threshold: float = 0.5,
    intro_title: str = "Introdu\u00e7\u00e3o",
) -> List[Dict[str, Any]]:
    """
    Detects chapter start times from Whisper segments.

    Parameters
    ----------
    segments              : list of Whisper segment dicts (start, end, text)
    total_duration        : total video duration in seconds
    min_chapter_duration  : minimum allowed chapter length in seconds
    max_chapters          : hard cap on number of chapters
    silence_threshold     : inter-segment pause (s) that triggers a boundary
    low_word_rate_threshold: wps below which a new segment might be a chapter
    intro_title           : title given to the very first chapter (0:00)

    Returns
    -------
    List of {"start": float, "title": str} - always starts at 0:00.
    """
    if not segments or total_duration < min_chapter_duration * 2:
        return [{"start": 0.0, "title": intro_title}]

    candidates: list[tuple[float, str]] = []
    for i, seg in enumerate(segments):
        if i == 0:
            continue
        prev = segments[i - 1]
        gap = seg["start"] - prev["end"]
        prev_text = str(prev.get("text", ""))
        is_silence_break = gap >= silence_threshold
        ends_sentence = bool(re.search(r"[.!?…]\s*$", prev_text))
        window_end = min(total_duration, seg["start"] + 10.0)
        rate_after = _word_rate(segments, seg["start"], window_end)
        is_low_rate = rate_after < low_word_rate_threshold
        if is_silence_break or (ends_sentence and is_low_rate):
            candidates.append((seg["start"], str(seg.get("text", ""))))

    chapters: List[Dict[str, Any]] = [{"start": 0.0, "title": intro_title}]
    last_start = 0.0
    for cand_start, cand_text in candidates:
        if cand_start - last_start < min_chapter_duration:
            continue
        title = _clean_title(cand_text)
        if not title:
            continue
        chapters.append({"start": cand_start, "title": title})
        last_start = cand_start
        if len(chapters) >= max_chapters:
            break

    return chapters


def chapters_to_youtube_text(
    chapters: List[Dict[str, Any]],
    video_title: Optional[str] = None,
) -> str:
    """Returns a YouTube-ready chapter list string (0:00 must be first)."""
    if not chapters:
        return ""
    lines: List[str] = []
    if video_title:
        lines.append(f"\U0001f4cc Cap\u00edtulos \u2014 {video_title}")
        lines.append("")
    for ch in chapters:
        ts = _fmt_yt(float(ch["start"]))
        title = str(ch.get("title", "Cap\u00edtulo"))
        lines.append(f"{ts} {title}")
    return "\n".join(lines)


def write_chapters_file(
    chapters: List[Dict[str, Any]],
    output_path: str,
    video_title: Optional[str] = None,
) -> str:
    """Writes YouTube-format chapters text file and returns the path."""
    from pathlib import Path
    content = chapters_to_youtube_text(chapters, video_title=video_title)
    Path(output_path).write_text(content, encoding="utf-8")
    return output_path


def write_chapters_json(
    chapters: List[Dict[str, Any]],
    output_path: str,
) -> str:
    """Writes chapters as enriched JSON file and returns the path."""
    import json
    from pathlib import Path
    enriched = [
        {**ch, "timestamp": _fmt_yt(float(ch["start"]))}
        for ch in chapters
    ]
    Path(output_path).write_text(
        json.dumps(enriched, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return output_path
