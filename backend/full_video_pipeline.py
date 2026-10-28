# -*- coding: utf-8 -*-
"""
Full Video Pipeline: Subtitling & Timeline Sticker Overlays for Long-Form Videos.

Processes full-length videos, generating animated/styled subtitles (with optional
SRT export) and seamlessly overlaying call-to-action stickers (Subscribe, Like,
Comment, Share) across the timeline at balanced intervals, powered by GPU NVENC.
"""
from __future__ import annotations

import json
import time
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .config import (
    ASPECT_RATIOS,
    DEFAULT_WHISPER_MODEL,
    OUTPUTS_DIR,
    STICKERS_DIR,
    USE_NVENC,
)
from .errors import PipelineCancelled
from . import (
    chapter_generator,
    downloader,
    metadata_generator,
    sticker_generator,
    subtitle_generator,
    transcriber,
    video_renderer,
)

UpdateCallback = Callable[[float, str], None]


@dataclass
class FullVideoOptions:
    whisper_model: str = DEFAULT_WHISPER_MODEL
    language: Optional[str] = None
    burn_subtitles: bool = True
    subtitle_style: str = "youtube_clean"
    subtitle_position: str = "bottom"  # bottom, center, top
    words_per_group: int = 3
    use_emojis: bool = False
    aspect_ratio: str = "original"  # original, 16:9, 9:16, 1:1

    # Stickers / Adesivos
    use_stickers: bool = True
    enabled_stickers: List[str] = field(
        default_factory=lambda: ["subscribe", "like", "comment", "share"]
    )
    sticker_interval_mode: str = "auto"  # auto, interval_60, interval_120, interval_180, interval_300, custom
    sticker_custom_interval: float = 90.0
    sticker_duration: float = 4.5
    sticker_position: str = "bottom_right"  # bottom_right, bottom_left, top_right, center_bottom

    # Hardware & exports
    use_nvenc: bool = USE_NVENC
    export_srt: bool = True
    export_chapters: bool = True
    chapter_min_duration: float = 60.0  # minimum chapter length in seconds

    # YouTube publishing package (titles/description/hashtags)
    use_ollama: bool = False
    ollama_model: str = "llama3"
    output_language: str = "auto"  # auto, pt, es, en

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "FullVideoOptions":
        data = data or {}
        opt = cls()

        for key in (
            "whisper_model", "language", "subtitle_style", "subtitle_position",
            "aspect_ratio", "sticker_interval_mode", "sticker_position",
            "ollama_model", "output_language",
        ):
            val = data.get(key)
            if isinstance(val, str) and val.strip():
                setattr(opt, key, val.strip())

        for key in ("export_chapters", "use_ollama"):
            val = data.get(key)
            if val is not None:
                setattr(opt, key, bool(val))

        ch_min = data.get("chapter_min_duration")
        if ch_min is not None:
            try:
                opt.chapter_min_duration = max(30.0, float(ch_min))
            except (ValueError, TypeError):
                pass

        for key in ("burn_subtitles", "use_emojis", "use_stickers", "use_nvenc", "export_srt"):
            val = data.get(key)
            if val is not None:
                setattr(opt, key, bool(val))

        words = data.get("words_per_group")
        if words is not None:
            try:
                opt.words_per_group = max(1, min(6, int(words)))
            except (ValueError, TypeError):
                pass

        c_int = data.get("sticker_custom_interval")
        if c_int is not None:
            try:
                opt.sticker_custom_interval = max(15.0, float(c_int))
            except (ValueError, TypeError):
                pass

        s_dur = data.get("sticker_duration")
        if s_dur is not None:
            try:
                opt.sticker_duration = max(2.5, min(10.0, float(s_dur)))
            except (ValueError, TypeError):
                pass

        stk_list = data.get("enabled_stickers")
        if isinstance(stk_list, list) and stk_list:
            opt.enabled_stickers = [str(s).strip().lower() for s in stk_list if str(s).strip()]

        return opt


def _check_cancel(cancel_event: Optional[threading.Event]) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise PipelineCancelled("Processamento de vídeo cancelado pelo usuário.")


def process_full_video_job(
    job_id: str,
    options: FullVideoOptions,
    update: UpdateCallback,
    source_url: Optional[str] = None,
    video_meta: Optional[Dict[str, Any]] = None,
    cancel_event: Optional[threading.Event] = None,
) -> Dict[str, Any]:
    """
    Executes the full video enrichment pipeline:
    1. Ingest input video (file or URL)
    2. Transcribe speech with Whisper (GPU CUDA)
    3. Generate .ass animated/styled subtitles & standalone .srt file
    4. Calculate balanced sticker timeline (Subscribe, Like, Comment, Share)
    5. Render full-length enriched MP4 via FFmpeg + NVENC
    6. Extract thumbnail and package final results.
    """
    started_at = time.time()
    output_dir = OUTPUTS_DIR / job_id
    output_dir.mkdir(parents=True, exist_ok=True)

    # 1. Video Ingestion
    if not video_meta and not source_url:
        raise ValueError("É necessário enviar um vídeo ou informar uma URL.")

    if not video_meta:
        update(1.0, "Iniciando download do vídeo...")

        def download_progress(percent: float, message: str) -> None:
            _check_cancel(cancel_event)
            update(1.0 + (percent / 100.0) * 8.0, message)

        video_meta = downloader.download_video(source_url, download_progress)

    video_path = video_meta.get("filepath", "")
    if not video_path or not Path(video_path).exists():
        raise FileNotFoundError(f"Arquivo de vídeo não encontrado: {video_path}")

    duration = float(video_meta.get("duration") or 0.0)
    if duration <= 0:
        duration = video_renderer.get_video_duration(video_path)

    orig_w, orig_h = video_renderer.get_video_dimensions(video_path)
    update(10.0, f"Vídeo pronto: {video_meta.get('title', 'Vídeo')} ({orig_w}x{orig_h}, {duration:.1f}s)")
    _check_cancel(cancel_event)

    # 2. Audio Transcription
    transcript: Dict[str, Any] = {"segments": [], "words": [], "language": "pt", "full_text": ""}
    if options.burn_subtitles or options.export_srt:
        update(12.0, "Carregando modelo Whisper para transcrição...")

        def transcribe_progress(pct: float, msg: str) -> None:
            _check_cancel(cancel_event)
            update(12.0 + (max(0.0, min(85.0, pct)) / 85.0) * 40.0, msg)

        transcript = transcriber.transcribe_audio(
            video_path,
            model_size=options.whisper_model,
            language=options.language,
            total_duration=duration,
            progress_callback=transcribe_progress,
        )
        _check_cancel(cancel_event)

        (output_dir / "transcript.json").write_text(
            json.dumps(transcript, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    segments = transcript.get("segments", [])
    words = transcript.get("words", [])
    update(54.0, f"Transcrição concluída: {len(segments)} falas ({transcript.get('language')}).")

    # 3. Export SRT
    srt_path = None
    if options.export_srt and segments:
        srt_file = output_dir / "legendas.srt"
        subtitle_generator.generate_srt_subtitles(segments, str(srt_file))
        srt_path = str(srt_file.resolve())

    # 3b. Detect & export chapters
    chapters: List[Dict[str, Any]] = []
    chapters_txt_path = None
    chapters_json_path = None
    if options.export_chapters and segments:
        update(55.0, "Detectando capítulos automáticos...")
        video_title = (video_meta or {}).get("title") or None
        chapters = chapter_generator.detect_chapters(
            segments=segments,
            total_duration=duration,
            min_chapter_duration=options.chapter_min_duration,
        )
        if len(chapters) > 1:  # only write if more than just intro
            ch_txt = output_dir / "capitulos.txt"
            ch_json = output_dir / "capitulos.json"
            chapter_generator.write_chapters_file(chapters, str(ch_txt), video_title=video_title)
            chapter_generator.write_chapters_json(chapters, str(ch_json))
            chapters_txt_path = str(ch_txt.resolve())
            chapters_json_path = str(ch_json.resolve())
            update(55.5, f"{len(chapters)} capítulo(s) detectado(s) automaticamente.")

    # 3c. YouTube Publishing Package (titles + description + hashtags)
    youtube_metadata: Optional[Dict[str, Any]] = None
    if segments:
        update(55.8, "Gerando pacote de publicação (títulos, descrição e hashtags)...")
        try:
            youtube_metadata = metadata_generator.generate_video_metadata(
                transcript=transcript,
                chapters=chapters,
                duration=duration,
                video_title=(video_meta or {}).get("title"),
                use_ollama=options.use_ollama,
                ollama_model=options.ollama_model,
                output_language=options.output_language,
            )
            meta_txt = output_dir / "youtube_metadata.txt"
            meta_txt.write_text(
                youtube_metadata["copy_paste_block"], encoding="utf-8"
            )
            (output_dir / "metadata.json").write_text(
                json.dumps(youtube_metadata, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            update(
                55.9,
                f"Pacote de publicação pronto: {len(youtube_metadata['titles'])} títulos sugeridos "
                f"(motor: {youtube_metadata['source']}).",
            )
        except Exception as exc:  # never let SEO generation break the render
            print(f"[!] metadata_generator falhou: {exc}")
            youtube_metadata = None

    # 4. Generate ASS Subtitles
    ass_path = None
    if options.burn_subtitles and (words or segments):
        update(56.0, "Gerando legendas sincronizadas...")
        ass_file = output_dir / "subtitles.ass"

        # Determine target resolution for subtitle scale
        if options.aspect_ratio in ASPECT_RATIOS and options.aspect_ratio != "original":
            play_x, play_y = ASPECT_RATIOS[options.aspect_ratio]
        else:
            play_x, play_y = orig_w, orig_h

        # Calculate vertical margin based on position
        if options.subtitle_position == "center":
            v_margin = int(play_y * 0.45)
        elif options.subtitle_position == "top":
            v_margin = int(play_y * 0.82)
        else:
            # bottom
            v_margin = int(play_y * 0.08) if play_y <= 1080 else 100

        subtitle_generator.generate_ass_subtitles(
            words=words,
            output_path=str(ass_file),
            clip_start=0.0,
            clip_end=duration + 1.0,
            style_name=options.subtitle_style,
            words_per_group=options.words_per_group,
            play_res_x=play_x,
            play_res_y=play_y,
            margin_v=v_margin,
            use_emojis=options.use_emojis,
            total_duration=duration,
        )
        ass_path = str(ass_file.resolve())

    # 5. Timeline Sticker Schedule
    sticker_events: List[Dict[str, Any]] = []
    if options.use_stickers and options.enabled_stickers:
        update(58.0, "Calculando posicionamento dos adesivos na timeline...")
        sticker_events = sticker_generator.calculate_sticker_timeline(
            total_duration=duration,
            enabled_stickers=options.enabled_stickers,
            interval_mode=options.sticker_interval_mode,
            custom_interval=options.sticker_custom_interval,
            sticker_duration=options.sticker_duration,
            position=options.sticker_position,
            stickers_dir=STICKERS_DIR,
        )
        _check_cancel(cancel_event)

    # 6. Video Rendering
    output_video_path = output_dir / "video_legendado.mp4"
    update(60.0, "Iniciando renderização com aceleração por hardware...")

    def render_progress(pct: float, msg: str) -> None:
        _check_cancel(cancel_event)
        mapped = 60.0 + (pct / 100.0) * 38.0
        update(mapped, msg)

    render_result = video_renderer.render_full_video(
        video_path=video_path,
        output_path=str(output_video_path),
        subtitle_path=ass_path,
        sticker_events=sticker_events,
        use_nvenc=options.use_nvenc,
        progress_cb=render_progress,
        cancel_event=cancel_event,
        target_aspect=options.aspect_ratio,
    )
    _check_cancel(cancel_event)

    # 7. Extract Thumbnail
    thumb_path = output_dir / "thumbnail.jpg"
    thumb_extracted = video_renderer.extract_thumbnail(
        str(output_video_path),
        str(thumb_path),
        at_seconds=min(5.0, duration * 0.2),
    )

    elapsed = round(time.time() - started_at, 1)
    update(100.0, "Vídeo legendado e enriquecido com sucesso!")

    result_data = {
        "job_id": job_id,
        "mode": "full_video",
        "status": "completed",
        "video": {
            **video_meta,
            "duration": round(duration, 2),
            "width": render_result.get("width", orig_w),
            "height": render_result.get("height", orig_h),
        },
        "output_file": {
            "filename": output_video_path.name,
            "url": f"/outputs/{job_id}/{output_video_path.name}",
            "size_bytes": output_video_path.stat().st_size,
            "duration": render_result.get("duration", round(duration, 2)),
            "encoder": render_result.get("encoder", "libx264"),
        },
        "srt_file": {
            "filename": "legendas.srt",
            "url": f"/outputs/{job_id}/legendas.srt",
        } if srt_path and Path(srt_path).exists() else None,
        "chapters_file": {
            "filename": "capitulos.txt",
            "url": f"/outputs/{job_id}/capitulos.txt",
        } if chapters_txt_path and Path(chapters_txt_path).exists() else None,
        "chapters_json": {
            "filename": "capitulos.json",
            "url": f"/outputs/{job_id}/capitulos.json",
        } if chapters_json_path and Path(chapters_json_path).exists() else None,
        "chapters": chapters,
        "youtube_metadata": youtube_metadata,
        "metadata_file": {
            "filename": "youtube_metadata.txt",
            "url": f"/outputs/{job_id}/youtube_metadata.txt",
        } if youtube_metadata else None,
        "thumbnail_url": f"/outputs/{job_id}/thumbnail.jpg" if thumb_extracted else None,
        "language": transcript.get("language"),
        "stickers_count": len(sticker_events),
        "stickers_timeline": [
            {
                "id": ev["id"],
                "type": ev["type"],
                "label": ev["label"],
                "start_time": ev["start_time"],
                "end_time": ev["end_time"],
                "start_formatted": f"{int(ev['start_time']//60):02d}:{int(ev['start_time']%60):02d}",
            }
            for ev in sticker_events
        ],
        "options": {
            "burn_subtitles": options.burn_subtitles,
            "subtitle_style": options.subtitle_style,
            "subtitle_position": options.subtitle_position,
            "use_stickers": options.use_stickers,
            "sticker_interval_mode": options.sticker_interval_mode,
            "sticker_position": options.sticker_position,
            "enabled_stickers": options.enabled_stickers,
            "export_chapters": options.export_chapters,
            "chapter_min_duration": options.chapter_min_duration,
        },
        "message": f"Vídeo completo pronto! {len(sticker_events)} adesivo(s) adicionados.",
        "elapsed_seconds": elapsed,
        "output_dir": str(output_dir.resolve()),
    }

    (output_dir / "result.json").write_text(
        json.dumps(result_data, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    return result_data
