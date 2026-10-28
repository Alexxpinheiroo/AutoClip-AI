"""Animated ASS captions plus hook/CTA overlays.

The generator receives word timestamps from Whisper and can optionally remap
them through the auto-edit timeline (when long silences were removed), so
captions stay perfectly in sync with jump-cut clips.
"""
from __future__ import annotations

import re
import textwrap
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

TimeMapper = Optional[Callable[[float], float]]


def format_ass_time(seconds: float) -> str:
    """Converts seconds into ASS format: H:MM:SS.cs"""
    seconds = max(0.0, float(seconds))
    hrs = int(seconds // 3600)
    mins = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    centis = int(round((seconds - int(seconds)) * 100))
    if centis >= 100:
        centis = 99
    return f"{hrs}:{mins:02d}:{secs:02d}.{centis:02d}"


SUBTITLE_STYLES = {
    "hormozi_yellow": {
        "primary": "&H00FFFFFF&",
        "highlight": "&H0000FFFF&",  # Bright Yellow (BBGGRR in ASS)
        "outline": "&H00000000&",
        "accent": "&H0000FFFF&",
        "fontsize": 68,
        "fontname": "Arial Black",
        "bold": 1,
        "margin_v": 240,
    },
    "mrbeast_green": {
        "primary": "&H00FFFFFF&",
        "highlight": "&H0000FF00&",
        "outline": "&H00000000&",
        "accent": "&H0000FF00&",
        "fontsize": 72,
        "fontname": "Impact",
        "bold": 1,
        "margin_v": 240,
    },
    "cyber_cyan": {
        "primary": "&H00FFFFFF&",
        "highlight": "&H00FFFF00&",
        "outline": "&H00220022&",
        "accent": "&H00FFFF00&",
        "fontsize": 68,
        "fontname": "Arial Black",
        "bold": 1,
        "margin_v": 240,
    },
    "minimal_white": {
        "primary": "&H00FFFFFF&",
        "highlight": "&H00E0E0E0&",
        "outline": "&H00000000&",
        "accent": "&H00E0E0E0&",
        "fontsize": 60,
        "fontname": "Arial",
        "bold": 1,
        "margin_v": 220,
    },
}

EMOJI_MAP = {
    "dinheiro": "💰", "lucro": "💰", "money": "💰", "profit": "💰", "milhao": "💰",
    "milhão": "💰", "riqueza": "💰", "investimento": "📈", "faturamento": "📈",
    "amor": "❤️", "love": "❤️", "medo": "😱", "scared": "😱", "insano": "🤯",
    "louco": "🤯", "crazy": "🤯", "insane": "🤯", "incrivel": "🤩", "incrível": "🤩",
    "amazing": "🤩", "melhor": "🔥", "best": "🔥", "segredo": "🤫", "secret": "🤫",
    "atencao": "⚠️", "atenção": "⚠️", "cuidado": "⚠️", "risada": "😂", "rindo": "😂",
    "engracado": "😂", "engraçado": "😂", "kkk": "😂", "haha": "😂", "funny": "😂",
    "sucesso": "🏆", "vitoria": "🏆", "vitória": "🏆", "win": "🏆", "erro": "❌",
    "mistake": "❌", "dica": "💡", "tip": "💡", "rapido": "⚡", "rápido": "⚡",
    "fast": "⚡", "gol": "⚽", "goal": "⚽", "jogo": "🎮", "game": "🎮",
    "polemica": "👀", "polêmica": "👀", "verdade": "👀", "truth": "👀",
}


def _escape_ass(text: str) -> str:
    return (
        str(text or "")
        .replace("\\", r"\\")
        .replace("{", r"\{")
        .replace("}", r"\}")
        .replace("\n", " ")
        .strip()
    )


def _wrap_ass(text: str, width: int = 22, max_lines: int = 2) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    if not text:
        return ""
    lines = textwrap.wrap(text, width=width, break_long_words=False, break_on_hyphens=False)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1].rstrip(".,;:!?") + "…"
    return r"\N".join(_escape_ass(line) for line in lines)


def _emoji_for(word: str) -> str:
    token = re.sub(r"[^a-zA-ZÀ-ÿ0-9]", "", word).lower()
    if not token:
        return ""
    for key, emoji in EMOJI_MAP.items():
        if token == key:
            return emoji
    return ""


def _group_words(
    words: List[Dict[str, Any]],
    words_per_group: int,
    max_chars: int = 22,
) -> List[List[Dict[str, Any]]]:
    groups: List[List[Dict[str, Any]]] = []
    current: List[Dict[str, Any]] = []
    for word in words:
        current.append(word)
        text_len = sum(len(w["word"]) + 1 for w in current)
        ends_sentence = bool(re.search(r"[.!?,;:]$", word["word"]))
        if len(current) >= words_per_group or text_len >= max_chars or ends_sentence:
            groups.append(current)
            current = []
    if current:
        groups.append(current)
    return groups


def generate_ass_subtitles(
    words: List[Dict[str, Any]],
    output_path: str,
    clip_start: float,
    clip_end: float,
    style_name: str = "hormozi_yellow",
    words_per_group: int = 2,
    play_res_x: int = 1080,
    play_res_y: int = 1920,
    font_scale: float = 1.0,
    margin_v: int = None,
    margin_h: int = 60,
    uppercase: bool = True,
    hook_text: Optional[str] = None,
    cta_text: Optional[str] = None,
    use_emojis: bool = True,
    time_mapper: TimeMapper = None,
    total_duration: Optional[float] = None,
) -> str:
    """Generates animated word-by-word captions plus optional hook/CTA.

    ``time_mapper`` maps an absolute source timestamp to the edited clip
    timeline (used when silences are removed). ``total_duration`` is the final
    rendered duration; when omitted it is inferred from the mapped words.
    """
    cfg = SUBTITLE_STYLES.get(style_name, SUBTITLE_STYLES["hormozi_yellow"])
    font_size = max(12, int(round(cfg["fontsize"] * font_scale)))
    bottom_margin = int(margin_v) if margin_v is not None else cfg["margin_v"]
    scale = max(0.65, min(1.35, min(play_res_x, play_res_y) / 1080.0))

    clip_words: List[Dict[str, Any]] = []
    for word in words:
        if word["end"] <= clip_start or word["start"] >= clip_end:
            continue
        raw = str(word.get("word", "")).strip()
        if not raw:
            continue
        if time_mapper is not None:
            rel_start = float(time_mapper(word["start"]))
            rel_end = float(time_mapper(word["end"]))
        else:
            rel_start = max(0.0, float(word["start"]) - clip_start)
            rel_end = max(rel_start + 0.05, float(word["end"]) - clip_start)
        if total_duration is not None:
            rel_start = max(0.0, min(float(total_duration), rel_start))
            rel_end = max(0.0, min(float(total_duration), rel_end))
        if rel_end - rel_start < 0.04:
            rel_end = rel_start + 0.08
        text = raw.upper() if uppercase else raw
        clip_words.append({
            "word": _escape_ass(text),
            "raw": text,
            "start": rel_start,
            "end": rel_end,
        })

    clip_words.sort(key=lambda item: item["start"])

    # ------------------------------------------------------------ header
    hook_size = int(round(58 * scale))
    cta_size = int(round(48 * scale))
    hook_margin = int(round(64 * scale))
    cta_margin = int(round(72 * scale))
    accent = cfg.get("accent", "&H0000FFFF&")
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {play_res_x}
PlayResY: {play_res_y}
ScaledBorderAndShadow: yes
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{cfg['fontname']},{font_size},{cfg['primary']},&H000000FF&,{cfg['outline']},&H80000000&,{cfg['bold']},0,0,0,100,100,2,0,1,5,3,2,{margin_h},{margin_h},{bottom_margin},1
Style: Hook,{cfg['fontname']},{hook_size},{cfg['primary']},&H000000FF&,{cfg['outline']},&H80000000&,-1,0,0,0,100,100,1.5,0,1,5,2,8,{margin_h},{margin_h},{hook_margin},1
Style: CTA,{cfg['fontname']},{cta_size},&H00000000&,&H000000FF&,{accent},{accent},-1,0,0,0,100,100,1,0,3,9,0,8,{margin_h},{margin_h},{cta_margin},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    if not clip_words:
        Path(output_path).write_text(header, encoding="utf-8")
        return output_path

    duration = float(total_duration) if total_duration is not None else max(w["end"] for w in clip_words)
    dialogue_lines: List[str] = []

    # ------------------------------------------------------------- hook
    hook = re.sub(r"\s+", " ", hook_text or "").strip()
    if hook:
        hook_display = _wrap_ass(hook.upper(), width=24, max_lines=2)
        hook_duration = min(3.2, max(1.6, duration * 0.24))
        hook_size_override = hook_size
        if len(hook) > 46:
            hook_size_override = int(hook_size * 0.82)
        dialogue_lines.append(
            f"Dialogue: 1,{format_ass_time(0.0)},{format_ass_time(hook_duration)},Hook,,0,0,0,,"
            + r"{\fad(180,220)\fs" + str(hook_size_override) + r"}" + hook_display
        )

    # -------------------------------------------------------------- CTA
    cta = re.sub(r"\s+", " ", cta_text or "").strip()
    if cta:
        cta_display = _wrap_ass(cta.upper(), width=24, max_lines=1)
        cta_duration = min(2.8, max(1.5, duration * 0.22))
        cta_start = max(0.0, duration - cta_duration)
        dialogue_lines.append(
            f"Dialogue: 1,{format_ass_time(cta_start)},{format_ass_time(duration)},CTA,,0,0,0,,"
            + r"{\fad(150,180)}" + cta_display
        )

    # ---------------------------------------------------------- captions
    groups = _group_words(clip_words, max(1, int(words_per_group)))
    emoji_budget = max(2, len(clip_words) // 9)
    emoji_used = 0

    for group in groups:
        for active_idx, active_word in enumerate(group):
            w_start = active_word["start"]
            w_end = active_word["end"]
            if active_idx < len(group) - 1:
                w_end = min(w_end, group[active_idx + 1]["start"])
            w_end = max(w_start + 0.06, w_end)

            line_parts: List[str] = []
            for idx, word in enumerate(group):
                text_word = word["word"]
                emoji = ""
                if use_emojis and emoji_used < emoji_budget:
                    emoji = _emoji_for(word.get("raw", ""))
                    if emoji:
                        emoji_used += 1
                if idx == active_idx:
                    line_parts.append(
                        r"{\fad(40,0)\c" + cfg["highlight"] + r"\fscx110\fscy110}" + text_word
                    )
                else:
                    line_parts.append(r"{\c" + cfg["primary"] + r"}" + text_word)
                if emoji:
                    # Segoe UI Emoji keeps the glyph readable on Windows; \r
                    # restores the caption style for the following words.
                    line_parts.append(r"{\fnSegoe UI Emoji\fs" + str(int(font_size * 0.9)) + r"}" + emoji + r"{\r}")

            rendered_text = " ".join(line_parts)
            dialogue_lines.append(
                f"Dialogue: 0,{format_ass_time(w_start)},{format_ass_time(w_end)},Default,,0,0,0,,{rendered_text}"
            )

    Path(output_path).write_text(header + "\n".join(dialogue_lines) + "\n", encoding="utf-8")
    return output_path
