# -*- coding: utf-8 -*-
"""
Sticker Generator & Timeline Placement Engine for AutoClip-AI.

Generates high-resolution transparent PNG stickers (Inscreva-se, Deixe o Like,
Deixe seu Comentário, Compartilhe) and calculates balanced timeline intervals
for long-form videos, assembling FFmpeg filter complex overlay graphs.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from PIL import Image, ImageDraw, ImageFont

STICKER_DEFINITIONS = {
    "subscribe": {
        "id": "subscribe",
        "label": "Inscreva-se no Canal",
        "filename": "sticker_subscribe.png",
        "color": (230, 33, 33),
        "border": (255, 45, 45),
        "title": "INSCREVA-SE NO CANAL",
        "subtitle": "Ative o sininho das notificações 🔔",
    },
    "like": {
        "id": "like",
        "label": "Deixe seu Like",
        "filename": "sticker_like.png",
        "color": (37, 99, 235),
        "border": (59, 130, 246),
        "title": "DEIXE SEU LIKE!",
        "subtitle": "Se está gostando, fortalece no like 👍",
    },
    "comment": {
        "id": "comment",
        "label": "Deixe seu Comentário",
        "filename": "sticker_comment.png",
        "color": (5, 150, 105),
        "border": (16, 185, 129),
        "title": "DEIXE SEU COMENTÁRIO",
        "subtitle": "Qual a sua opinião? Comente aqui embaixo! 💬",
    },
    "share": {
        "id": "share",
        "label": "Compartilhe o Vídeo",
        "filename": "sticker_share.png",
        "color": (124, 58, 237),
        "border": (139, 92, 246),
        "title": "COMPARTILHE O VÍDEO",
        "subtitle": "Envie esse conteúdo para seus amigos! 🚀",
    },
}


def _get_fonts(title_size: int = 23, sub_size: int = 14) -> Tuple[Any, Any]:
    font_candidates = [
        "arialbd.ttf",
        "seguisb.ttf",
        "calibrib.ttf",
        "DejaVuSans-Bold.ttf",
    ]
    sub_candidates = [
        "arial.ttf",
        "segoeui.ttf",
        "calibri.ttf",
        "DejaVuSans.ttf",
    ]
    f_title = None
    f_sub = None
    for fn in font_candidates:
        try:
            f_title = ImageFont.truetype(fn, title_size)
            break
        except Exception:
            continue
    for fn in sub_candidates:
        try:
            f_sub = ImageFont.truetype(fn, sub_size)
            break
        except Exception:
            continue
    return f_title or ImageFont.load_default(), f_sub or ImageFont.load_default()


def ensure_stickers(stickers_dir: Path) -> Dict[str, str]:
    """Generates default sticker assets if they do not exist."""
    stickers_dir.mkdir(parents=True, exist_ok=True)
    paths: Dict[str, str] = {}
    f_title, f_sub = _get_fonts()

    for s_id, s_def in STICKER_DEFINITIONS.items():
        dest = stickers_dir / s_def["filename"]
        paths[s_id] = str(dest.resolve())
        if dest.exists() and dest.stat().st_size > 1000:
            continue

        width, height = 530, 110
        img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)

        # Rounded dark frosted background
        d.rounded_rectangle(
            [4, 4, width - 4, height - 4],
            radius=24,
            fill=(16, 19, 28, 240),
            outline=s_def["border"] + (240,),
            width=3,
        )

        # Icon box
        if s_id == "subscribe":
            d.rounded_rectangle([22, 22, 88, 88], radius=18, fill=s_def["color"] + (255,))
            d.polygon([(46, 38), (46, 72), (72, 55)], fill=(255, 255, 255, 255))
            # Bell badge on right
            d.rounded_rectangle([460, 30, 506, 76], radius=12, fill=(35, 40, 56, 220))
            d.polygon([(483, 37), (473, 58), (493, 58)], fill=(255, 204, 0, 255))
            d.rectangle([471, 58, 495, 62], fill=(255, 204, 0, 255))
            d.ellipse([480, 62, 486, 68], fill=(255, 204, 0, 255))
        elif s_id == "like":
            d.ellipse([22, 22, 88, 88], fill=s_def["color"] + (255,))
            d.rectangle([38, 48, 48, 74], fill=(255, 255, 255, 255))
            d.rounded_rectangle([52, 40, 75, 74], radius=6, fill=(255, 255, 255, 255))
            d.polygon([(52, 48), (62, 33), (68, 35), (66, 48)], fill=(255, 255, 255, 255))
        elif s_id == "comment":
            d.ellipse([22, 22, 88, 88], fill=s_def["color"] + (255,))
            d.rounded_rectangle([36, 36, 74, 66], radius=8, fill=(255, 255, 255, 255))
            d.polygon([(42, 66), (42, 76), (54, 66)], fill=(255, 255, 255, 255))
            d.line([(44, 45), (66, 45)], fill=s_def["color"] + (255,), width=3)
            d.line([(44, 55), (60, 55)], fill=s_def["color"] + (255,), width=3)
        else:  # share
            d.ellipse([22, 22, 88, 88], fill=s_def["color"] + (255,))
            d.polygon([(52, 42), (72, 55), (52, 68)], fill=(255, 255, 255, 255))
            d.arc([36, 52, 62, 78], start=180, end=340, fill=(255, 255, 255, 255), width=5)

        # Typography
        d.text((106, 26), s_def["title"], fill=(255, 255, 255, 255), font=f_title)
        d.text((106, 58), s_def["subtitle"], fill=(205, 215, 235, 230), font=f_sub)

        img.save(dest, "PNG")

    return paths


def calculate_sticker_timeline(
    total_duration: float,
    enabled_stickers: Optional[List[str]] = None,
    interval_mode: str = "auto",
    custom_interval: float = 90.0,
    sticker_duration: float = 4.5,
    position: str = "bottom_right",
    stickers_dir: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """Calculates timestamps for stickers evenly across the timeline."""
    if not enabled_stickers:
        enabled_stickers = ["subscribe", "like", "comment", "share"]

    valid_stickers = [s for s in enabled_stickers if s in STICKER_DEFINITIONS]
    if not valid_stickers or total_duration < 10.0:
        return []

    times: List[float] = []
    dur = float(total_duration)

    if interval_mode == "auto":
        if dur <= 60.0:
            times = [dur * 0.22, dur * 0.78]
        elif dur <= 180.0:
            times = [20.0, dur * 0.50, dur * 0.85]
        elif dur <= 600.0:
            step = max(75.0, dur / (len(valid_stickers) + 1.2))
            t = 25.0
            while t < dur - 15.0:
                times.append(t)
                t += step
        else:
            t = 30.0
            step = 135.0
            while t < dur - 20.0:
                times.append(t)
                t += step
    elif interval_mode == "interval_60":
        t = 30.0
        while t < dur - 15.0:
            times.append(t)
            t += 60.0
    elif interval_mode == "interval_120":
        t = 45.0
        while t < dur - 15.0:
            times.append(t)
            t += 120.0
    elif interval_mode == "interval_180":
        t = 60.0
        while t < dur - 15.0:
            times.append(t)
            t += 180.0
    elif interval_mode == "interval_300":
        t = 90.0
        while t < dur - 15.0:
            times.append(t)
            t += 300.0
    else:  # custom
        step = max(15.0, float(custom_interval))
        t = min(30.0, step / 2.0)
        while t < dur - 15.0:
            times.append(t)
            t += step

    times = [t for t in times if t + sticker_duration <= dur - 0.5]
    if not times:
        if dur > sticker_duration + 5.0:
            times = [dur * 0.5]
        else:
            return []

    events: List[Dict[str, Any]] = []
    paths = ensure_stickers(stickers_dir) if stickers_dir else {}

    for idx, t_start in enumerate(times):
        stk_type = valid_stickers[idx % len(valid_stickers)]
        stk_def = STICKER_DEFINITIONS[stk_type]
        t_end = round(t_start + sticker_duration, 2)
        events.append({
            "id": idx + 1,
            "type": stk_type,
            "label": stk_def["label"],
            "title": stk_def["title"],
            "start_time": round(t_start, 2),
            "end_time": t_end,
            "duration": round(sticker_duration, 2),
            "position": position,
            "image_path": paths.get(stk_type, ""),
        })

    return events


def build_sticker_overlay_filter(
    sticker_events: List[Dict[str, Any]],
    video_width: int,
    video_height: int,
    start_input_index: int = 1,
    base_video_label: str = "[vsub]",
) -> Tuple[List[str], List[str], str]:
    """
    Builds the FFmpeg CLI input arguments and filter_complex parts
    to overlay all sticker events onto `base_video_label`.

    Returns (extra_input_args, filter_parts, final_video_label).
    """
    if not sticker_events:
        return [], [], base_video_label

    scale_factor = max(0.5, min(1.8, min(video_width, video_height) / 1080.0))
    sticker_w = int(round(500 * scale_factor))
    if sticker_w % 2 != 0:
        sticker_w += 1
    margin_x = int(round(45 * scale_factor))
    margin_y = int(round(55 * scale_factor))

    input_args: List[str] = []
    filter_parts: List[str] = []
    current_label = base_video_label

    fade_duration = 0.35

    for idx, evt in enumerate(sticker_events):
        inp_idx = start_input_index + idx
        img_path = evt["image_path"]
        st = float(evt["start_time"])
        et = float(evt["end_time"])
        dur = max(1.0, float(evt.get("duration") or (et - st)))
        fade_out_st = max(0.1, dur - fade_duration)

        input_args.extend(["-loop", "1", "-t", f"{dur + 0.5:.3f}", "-i", str(Path(img_path).resolve())])

        pos = evt.get("position", "bottom_right")
        if pos == "bottom_left":
            x_expr = f"{margin_x}"
            y_expr = f"H-h-{margin_y}"
        elif pos == "top_right":
            x_expr = f"W-w-{margin_x}"
            y_expr = f"{margin_y}"
        elif pos == "center_bottom":
            x_expr = "(W-w)/2"
            y_expr = f"H-h-{margin_y + int(90 * scale_factor)}"
        else:  # bottom_right
            x_expr = f"W-w-{margin_x}"
            y_expr = f"H-h-{margin_y}"

        stk_label = f"[stk{idx}]"
        next_label = f"[vstk{idx}]"

        filter_parts.append(
            f"[{inp_idx}:v]format=rgba,scale={sticker_w}:-2,"
            f"fade=t=in:st=0:d={fade_duration:.2f}:alpha=1,"
            f"fade=t=out:st={fade_out_st:.2f}:d={fade_duration:.2f}:alpha=1,"
            f"setpts=PTS-STARTPTS+{st:.3f}/TB{stk_label}"
        )

        filter_parts.append(
            f"{current_label}{stk_label}overlay={x_expr}:{y_expr}:eof_action=pass:enable='between(t,{st:.3f},{et:.3f})'{next_label}"
        )
        current_label = next_label

    return input_args, filter_parts, current_label
