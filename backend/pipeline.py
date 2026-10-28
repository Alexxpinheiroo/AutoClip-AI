import json
import time
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .config import (
    ASPECT_RATIOS,
    DEFAULT_ASPECT_RATIO,
    DEFAULT_CLIP_MAX_DURATION,
    DEFAULT_CLIP_MIN_DURATION,
    DEFAULT_WHISPER_MODEL,
    OUTPUTS_DIR,
    USE_NVENC,
)
from . import (
    analyzer,
    downloader,
    face_tracker,
    media_signals,
    subtitle_generator,
    transcriber,
    video_renderer,
)
from .errors import PipelineCancelled

UpdateCallback = Callable[[float, str], None]
TimeMapper = Callable[[float], float]


@dataclass
class PipelineOptions:
    num_clips: int = 3
    min_duration: float = float(DEFAULT_CLIP_MIN_DURATION)
    max_duration: float = float(DEFAULT_CLIP_MAX_DURATION)
    whisper_model: str = DEFAULT_WHISPER_MODEL
    language: Optional[str] = None
    subtitle_style: str = "hormozi_yellow"
    words_per_group: int = 2
    use_ollama: bool = False
    ollama_model: str = "llama3"
    use_face_tracking: bool = True
    use_nvenc: bool = USE_NVENC

    # OpusClip-style controls
    prompt: str = ""
    genre: str = "auto"
    aspect_ratio: str = DEFAULT_ASPECT_RATIO
    use_multimodal: bool = True
    remove_silences: bool = True
    silence_threshold: float = 0.55
    show_hook: bool = True
    show_cta: bool = True
    cta_text: str = "Siga para mais cortes!"
    use_emojis: bool = True

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "PipelineOptions":
        data = data or {}
        options = cls()
        for key in (
            "whisper_model", "language", "subtitle_style", "ollama_model",
            "prompt", "genre", "aspect_ratio", "cta_text",
        ):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                setattr(options, key, value.strip())

        if not options.prompt:
            options.prompt = ""
        options.prompt = options.prompt[:500]
        options.cta_text = options.cta_text[:80]

        for key in ("num_clips", "words_per_group"):
            value = data.get(key)
            if value is not None:
                try:
                    setattr(options, key, int(value))
                except (TypeError, ValueError):
                    pass

        for key in ("min_duration", "max_duration", "silence_threshold"):
            value = data.get(key)
            if value is not None:
                try:
                    setattr(options, key, float(value))
                except (TypeError, ValueError):
                    pass

        for key in (
            "use_ollama", "use_face_tracking", "use_nvenc", "use_multimodal",
            "remove_silences", "show_hook", "show_cta", "use_emojis",
        ):
            value = data.get(key)
            if value is not None:
                setattr(options, key, bool(value))

        options.num_clips = max(1, min(10, options.num_clips))
        options.min_duration = max(5.0, min(900.0, options.min_duration))
        options.max_duration = max(options.min_duration + 1.0, min(900.0, options.max_duration))
        options.words_per_group = max(1, min(5, options.words_per_group))
        options.silence_threshold = max(0.30, min(2.0, options.silence_threshold))
        if options.aspect_ratio not in ASPECT_RATIOS:
            options.aspect_ratio = DEFAULT_ASPECT_RATIO
        if options.genre not in analyzer.GENRE_PRESETS:
            options.genre = "auto"
        return options


def _check_cancel(cancel_event: Optional[threading.Event]) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise PipelineCancelled("Processamento cancelado pelo usuário.")


def _build_edit_plan(
    words: List[Dict[str, Any]],
    start: float,
    end: float,
    min_silence: float = 0.55,
    pad: float = 0.12,
) -> Optional[Tuple[List[Tuple[float, float]], TimeMapper, float]]:
    """Removes pauses longer than ``min_silence`` inside a clip.

    Returns ``(keep_intervals, time_mapper, edited_duration)`` or ``None`` when
    there is nothing meaningful to cut (or the edit would shrink the clip too
    aggressively).
    """
    if not words:
        return None

    spans: List[Tuple[float, float]] = []
    for word in words:
        a = max(start, float(word.get("start", 0.0)))
        b = min(end, float(word.get("end", 0.0)))
        if b - a <= 0.02:
            continue
        if spans and a - spans[-1][1] <= 0.18:
            spans[-1] = (spans[-1][0], max(spans[-1][1], b))
        else:
            spans.append((a, b))

    if not spans:
        return None

    # Natural short pauses stay in the video; only long dead air is removed.
    kept: List[Tuple[float, float]] = []
    for a, b in spans:
        if kept and a - kept[-1][1] <= min_silence:
            kept[-1] = (kept[-1][0], max(kept[-1][1], b))
        else:
            kept.append((a, b))

    padded: List[Tuple[float, float]] = []
    for a, b in kept:
        a2 = max(start, a - pad)
        b2 = min(end, b + pad)
        if padded and a2 - padded[-1][1] <= 0.05:
            padded[-1] = (padded[-1][0], max(padded[-1][1], b2))
        else:
            padded.append((a2, b2))

    kept = padded
    if not kept or len(kept) > 60:
        return None

    full = max(0.1, end - start)
    edited = sum(b - a for a, b in kept)
    if edited < max(6.0, full * 0.45) or edited >= full * 0.985:
        return None

    def mapper(source_time: float) -> float:
        target = 0.0
        for a, b in kept:
            if source_time <= a:
                return target
            if source_time < b:
                return target + (source_time - a)
            target += b - a
        return target

    return kept, mapper, edited


def _settings_snapshot(options: PipelineOptions) -> Dict[str, Any]:
    return {
        "num_clips": options.num_clips,
        "min_duration": options.min_duration,
        "max_duration": options.max_duration,
        "prompt": options.prompt,
        "genre": options.genre,
        "aspect_ratio": options.aspect_ratio,
        "use_multimodal": options.use_multimodal,
        "remove_silences": options.remove_silences,
        "show_hook": options.show_hook,
        "show_cta": options.show_cta,
        "use_emojis": options.use_emojis,
    }


def process_job(
    job_id: str,
    options: PipelineOptions,
    update: UpdateCallback,
    source_url: Optional[str] = None,
    video_meta: Optional[Dict[str, Any]] = None,
    cancel_event: Optional[threading.Event] = None,
) -> Dict[str, Any]:
    """
    Full AutoClip pipeline:
    download/upload -> transcribe -> multimodal analysis -> prompt-aware
    selection -> auto edit (silence removal) -> reframe -> hook/CTA/captions
    -> render.
    """
    started_at = time.time()
    output_dir = OUTPUTS_DIR / job_id
    output_dir.mkdir(parents=True, exist_ok=True)

    if not video_meta and not source_url:
        raise ValueError("É necessário enviar um vídeo ou informar uma URL.")

    # ---------------------------------------------------------------- download
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

    update(10.0, f"Vídeo pronto: {video_meta.get('title', 'sem título')} ({duration:.1f}s)")
    _check_cancel(cancel_event)

    # ------------------------------------------------------------- transcribe
    def transcribe_progress(pct: float, msg: str) -> None:
        _check_cancel(cancel_event)
        update(10.0 + (max(0.0, min(85.0, pct)) / 85.0) * 40.0, msg)

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
    update(51.0, f"Transcrição concluída: {len(segments)} trechos, idioma {transcript.get('language')}.")

    if not segments:
        result = {
            "job_id": job_id,
            "status": "completed",
            "video": {**video_meta, "duration": round(duration, 2)},
            "language": transcript.get("language"),
            "clips": [],
            "message": "Nenhuma fala detectada no vídeo — não foi possível gerar cortes.",
            "settings": _settings_snapshot(options),
            "elapsed_seconds": round(time.time() - started_at, 1),
        }
        (output_dir / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        update(100.0, result["message"])
        return result

    # ------------------------------------------------------ multimodal signals
    signals: Optional[Dict[str, Any]] = None
    if options.use_multimodal:
        update(52.0, "Analisando áudio, energia e cortes de cena...")
        try:
            signals = media_signals.analyze(video_path, total_duration=duration)
            scene_count = len(signals.get("scene_cuts") or [])
            update(55.0, f"Sinais extraídos: {scene_count} mudanças de cena detectadas.")
        except Exception as exc:
            print(f"[!] Análise multimodal indisponível ({exc}); seguindo só com a transcrição.")
            signals = None
        _check_cancel(cancel_event)

    # ---------------------------------------------------------------- analyze
    update(56.0, "Analisando momentos virais com o pedido do usuário...")
    clips = analyzer.analyze_video_highlights(
        segments,
        transcript.get("full_text", ""),
        target_count=options.num_clips,
        min_duration=options.min_duration,
        max_duration=options.max_duration,
        use_ollama=options.use_ollama,
        ollama_model=options.ollama_model,
        prompt=options.prompt,
        genre=options.genre,
        language=transcript.get("language"),
        signals=signals,
    )
    _check_cancel(cancel_event)

    if not clips:
        result = {
            "job_id": job_id,
            "status": "completed",
            "video": {**video_meta, "duration": round(duration, 2)},
            "language": transcript.get("language"),
            "clips": [],
            "message": "Não encontramos cortes com duração suficiente nesse vídeo.",
            "settings": _settings_snapshot(options),
            "elapsed_seconds": round(time.time() - started_at, 1),
        }
        (output_dir / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        update(100.0, result["message"])
        return result

    # Titles, hooks, descriptions and hashtags (LLM when enabled).
    update(59.0, "Gerando títulos, ganchos e hashtags...")
    clips = analyzer.enrich_clips(
        clips,
        prompt=options.prompt,
        genre=options.genre,
        language=transcript.get("language"),
        use_ollama=options.use_ollama,
        ollama_model=options.ollama_model,
    )
    _check_cancel(cancel_event)

    update(60.0, f"{len(clips)} momentos encontrados. Preparando cortes {options.aspect_ratio}...")

    # ------------------------------------------------------------- render loop
    words = transcript.get("words", [])
    total_clips = len(clips)
    rendered_clips: List[Dict[str, Any]] = []
    target_width, target_height = ASPECT_RATIOS[options.aspect_ratio]
    target_aspect = target_width / float(target_height)

    for index, clip in enumerate(clips):
        _check_cancel(cancel_event)
        clip_number = index + 1
        slice_start = 60.0 + (index / max(1, total_clips)) * 40.0
        slice_size = 40.0 / max(1, total_clips)

        start_time = max(0.0, float(clip.get("start_time", 0.0)))
        end_time = min(
            duration or float(clip.get("end_time", start_time + 30)),
            float(clip.get("end_time", start_time + 30)),
        )
        if end_time - start_time < 1.0:
            continue

        # Auto edit: remove long pauses inside the selected span.
        plan: Optional[Tuple[List[Tuple[float, float]], TimeMapper, float]] = None
        if options.remove_silences:
            plan = _build_edit_plan(
                words, start_time, end_time,
                min_silence=options.silence_threshold,
            )

        # Face tracking -> crop box for the selected aspect ratio.
        crop = None
        if options.use_face_tracking:
            update(slice_start + slice_size * 0.03, f"Corte {clip_number}/{total_clips}: reenquadrando...")
            try:
                crop = face_tracker.calculate_crop_coordinates(
                    video_path, start_time, end_time, target_aspect=target_aspect
                )
            except Exception as exc:
                print(f"[!] Rastreamento facial falhou ({exc}); usando corte central.")
                crop = None
        _check_cancel(cancel_event)

        ass_path = output_dir / f"clip_{clip_number:02d}.ass"
        output_path = output_dir / f"clip_{clip_number:02d}.mp4"

        def write_subtitles(current_plan: Optional[Tuple[List[Tuple[float, float]], TimeMapper, float]]) -> None:
            mapper = current_plan[1] if current_plan else None
            total = current_plan[2] if current_plan else (end_time - start_time)
            hook = None
            if options.show_hook:
                hook = clip.get("hook_title") or clip.get("title") or ""
            cta = options.cta_text if options.show_cta else None
            subtitle_generator.generate_ass_subtitles(
                words,
                str(ass_path),
                start_time,
                end_time,
                style_name=options.subtitle_style,
                words_per_group=options.words_per_group,
                play_res_x=target_width,
                play_res_y=target_height,
                hook_text=hook,
                cta_text=cta,
                use_emojis=options.use_emojis,
                time_mapper=mapper,
                total_duration=total,
            )

        update(slice_start + slice_size * 0.15, f"Corte {clip_number}/{total_clips}: gerando legendas...")
        write_subtitles(plan)
        _check_cancel(cancel_event)

        def render_progress(
            percent: float,
            message: str,
            _base: float = slice_start + slice_size * 0.22,
            _span: float = slice_size * 0.70,
        ) -> None:
            update(_base + (percent / 100.0) * _span, f"Corte {clip_number}/{total_clips}: {message}")

        update(slice_start + slice_size * 0.22, f"Corte {clip_number}/{total_clips}: renderizando...")
        try:
            render_info = video_renderer.render_clip(
                video_path,
                str(output_path),
                start_time,
                end_time,
                crop=crop,
                subtitle_path=str(ass_path),
                use_nvenc=options.use_nvenc,
                progress_cb=render_progress,
                cancel_event=cancel_event,
                target_width=target_width,
                target_height=target_height,
                keep_intervals=plan[0] if plan else None,
                edited_duration=plan[2] if plan else None,
            )
        except Exception as exc:
            if isinstance(exc, video_renderer.RenderCancelled) or (
                cancel_event is not None and cancel_event.is_set()
            ):
                raise
            if plan is None:
                raise
            # A filter/codec failure must never lose the clip: retry without
            # jump-cuts and rebuild subtitles for the untouched timeline.
            print(f"[!] Auto-edit falhou ({exc}); renderizando sem cortes de silêncio.")
            plan = None
            write_subtitles(None)
            render_info = video_renderer.render_clip(
                video_path,
                str(output_path),
                start_time,
                end_time,
                crop=crop,
                subtitle_path=str(ass_path),
                use_nvenc=options.use_nvenc,
                progress_cb=render_progress,
                cancel_event=cancel_event,
                target_width=target_width,
                target_height=target_height,
            )

        thumbnail = video_renderer.extract_thumbnail(
            render_info["path"],
            str(output_dir / f"clip_{clip_number:02d}.jpg"),
            at_seconds=min(1.0, max(0.1, render_info["duration"] * 0.2)),
        )

        rendered_clips.append({
            "id": clip_number,
            "filename": render_info["filename"],
            "path": render_info["path"],
            "url": f"/outputs/{job_id}/{render_info['filename']}",
            "thumbnail_url": f"/outputs/{job_id}/clip_{clip_number:02d}.jpg" if thumbnail else None,
            "title": clip.get("title") or f"Corte {clip_number}",
            "hook_title": clip.get("hook_title") or clip.get("title") or "",
            "description": clip.get("description") or "",
            "hashtags": clip.get("hashtags") or [],
            "start_time": round(start_time, 2),
            "end_time": round(end_time, 2),
            "duration": render_info.get("duration", round(end_time - start_time, 1)),
            "source_duration": round(end_time - start_time, 1),
            "virality_score": clip.get("virality_score", 0),
            "score_breakdown": clip.get("score_breakdown") or {},
            "reason": clip.get("reason", ""),
            "hook_text": clip.get("hook_text", ""),
            "matched_keywords": clip.get("matched_keywords") or [],
            "signals": clip.get("signals") or {},
            "aspect_ratio": options.aspect_ratio,
            "width": target_width,
            "height": target_height,
            "auto_edited": bool(render_info.get("auto_edited")),
            "edit_cuts": int(render_info.get("kept_intervals") or 1),
            "file_size_bytes": render_info.get("size_bytes", 0),
            "encoder": render_info.get("encoder", ""),
            "source": clip.get("source", "heuristic"),
        })

        update(slice_start + slice_size, f"Corte {clip_number}/{total_clips} pronto.")

    # ----------------------------------------------------------------- result
    result = {
        "job_id": job_id,
        "status": "completed",
        "video": {**video_meta, "duration": round(duration, 2)},
        "language": transcript.get("language"),
        "language_probability": transcript.get("language_probability"),
        "transcript": transcript.get("full_text", ""),
        "segments": segments,
        "clips": rendered_clips,
        "settings": _settings_snapshot(options),
        "output_dir": str(output_dir.resolve()),
        "elapsed_seconds": round(time.time() - started_at, 1),
    }
    (output_dir / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    update(100.0, f"Pronto! {len(rendered_clips)} corte(s) gerado(s).")
    return result
