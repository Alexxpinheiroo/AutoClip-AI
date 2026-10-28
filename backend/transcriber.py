# -*- coding: utf-8 -*-
"""
Transcriber - AutoClip-AI

State-of-the-art local transcription pipeline for bilingual (pt/es) content.

WHY THIS EXISTS (root causes of the old "bugged captions"):
  1. The default model was "base" (74M params) - far too weak for pt/es and
     notoriously confuses the two languages. large-v3 (1.55B) is ~10x more
     accurate on FLEURS for both and was already downloaded locally.
  2. faster-whisper issue #1476: with multilingual=True and
     condition_on_previous_text=True the previous window's tokens are used as
     the prompt BEFORE the per-window language switch, so Spanish gets decoded
     as broken Portuguese silently. The maintainers themselves call per-window
     multilingual redetection "a bit hacky" (issue #918) and recommend
     splitting audio into speech regions with a dedicated language-ID step.
  3. Vanilla Whisper language detection only looks at the FIRST 30 seconds of
     audio. An intro with music/noise poisons the detection for the whole file.

THE NEW PIPELINE (3 stages):
  Stage 1 - Multi-region Language ID (LID):
      Silero VAD finds speech regions; we sample up to 5 regions spread across
      the ENTIRE file (not just the first 30s) and aggregate Whisper's language
      posteriors. Optionally restricted to {pt, es} for rock-solid
      Portuguese/Spanish discrimination.
  Stage 2 - Full transcription with the dominant language pinned:
      condition_on_previous_text=False prevents cross-window prompt
      contamination (the #1476 failure mode). hallucination_silence_threshold
      drops the classic "subtitles repeat themselves" hallucination loops.
  Stage 3 - Code-switch rescue:
      Segments with low confidence (temperature fallback fired, low avg_logprob
      or high compression ratio) get their audio slice re-checked with LID; if
      another language is detected the slice is re-transcribed with the correct
      language and timestamps offset back into place.

Word-level timestamps are preserved for the karaoke/animated captions.
"""
from __future__ import annotations

import os
import threading
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

from .config import COMPUTE_TYPE, DEFAULT_WHISPER_MODEL, DEVICE

# ---------------------------------------------------------------------------
# Thread-safe model cache - one WhisperModel instance per model size
# ---------------------------------------------------------------------------
_model_cache: Dict[str, Any] = {}
_model_lock = threading.Lock()

SAMPLE_RATE = 16000

# Languages accepted as an explicit bilingual pair hint (smart code-switching).
BILINGUAL_PAIRS: Dict[str, Tuple[str, ...]] = {
    "pt-es": ("pt", "es"),
    "es-pt": ("es", "pt"),
}


def get_whisper_model(model_size: str = DEFAULT_WHISPER_MODEL):
    """Returns a cached WhisperModel for the given size, loaded on DEVICE."""
    from faster_whisper import WhisperModel

    if model_size not in _model_cache:
        with _model_lock:
            if model_size not in _model_cache:
                print(
                    f"[*] Carregando modelo Whisper '{model_size}' "
                    f"no dispositivo: {DEVICE} ({COMPUTE_TYPE})..."
                )
                _model_cache[model_size] = WhisperModel(
                    model_size,
                    device=DEVICE,
                    compute_type=COMPUTE_TYPE,
                    download_root=os.path.join(
                        os.path.dirname(__file__), "..", "models"
                    ),
                    cpu_threads=os.cpu_count() or 4,
                    num_workers=1,
                )
    return _model_cache[model_size]


# ---------------------------------------------------------------------------
# Stage 1 - Multi-region language identification
# ---------------------------------------------------------------------------
def _speech_regions(audio: np.ndarray) -> List[Dict[str, int]]:
    """Silero VAD over the whole file. Returns [{'start','end'}] in samples."""
    from faster_whisper.vad import VadOptions, get_speech_timestamps

    options = VadOptions(
        threshold=0.45,
        min_speech_duration_ms=300,
        min_silence_duration_ms=300,
        speech_pad_ms=200,
    )
    return get_speech_timestamps(audio, options, sampling_rate=SAMPLE_RATE)


def _pick_lid_windows(
    regions: List[Dict[str, int]],
    max_windows: int = 5,
    window_seconds: float = 25.0,
) -> List[Tuple[int, int]]:
    """
    Selects up to *max_windows* windows spread across the file so the language
    decision represents the WHOLE video, not just its first 30 seconds.
    """
    if not regions:
        return []
    max_len = int(window_seconds * SAMPLE_RATE)

    if len(regions) <= max_windows:
        chosen = regions
    else:
        # Evenly spaced picks: first, last and intermediate regions.
        idxs = np.linspace(0, len(regions) - 1, num=max_windows).round().astype(int)
        idxs = sorted(set(idxs.tolist()))
        chosen = [regions[i] for i in idxs]

    windows: List[Tuple[int, int]] = []
    for region in chosen:
        start = region["start"]
        end = min(region["end"], start + max_len)
        if end - start > SAMPLE_RATE:  # at least 1s of speech
            windows.append((start, end))
    return windows


def _detect_language_on_chunk(
    model: Any,
    audio: np.ndarray,
    start: int,
    end: int,
    restrict_to: Optional[Tuple[str, ...]],
) -> Tuple[Optional[str], float, Dict[str, float]]:
    """Runs Whisper LID on one audio slice, optionally restricted to a pair."""
    chunk = audio[max(0, start):min(len(audio), end)]
    if chunk.size < SAMPLE_RATE // 2:  # < 0.5s is useless for LID
        return None, 0.0, {}

    _lang, _prob, all_probs = model.detect_language(chunk)
    if not all_probs:
        return None, 0.0, {}

    if restrict_to:
        filtered = {l: p for l, p in all_probs if l in restrict_to}
        total = sum(filtered.values())
        if total <= 0:
            return None, 0.0, {}
        probs = {l: p / total for l, p in filtered.items()}
    else:
        probs = {l: p for l, p in all_probs}

    best = max(probs.items(), key=lambda kv: kv[1])
    return best[0], float(best[1]), probs


def detect_dominant_language(
    model: Any,
    audio: np.ndarray,
    restrict_to: Optional[Tuple[str, ...]] = None,
) -> Tuple[Optional[str], float, Dict[str, Any]]:
    """
    Aggregated multi-region language detection.

    Returns (language, confidence, details) where details includes per-window
    votes - useful for debugging and for the UI.
    """
    regions = _speech_regions(audio)
    windows = _pick_lid_windows(regions)

    if not windows:
        # No speech found by VAD - fall back to a single pass over the start.
        lang, prob, _ = _detect_language_on_chunk(model, audio, 0, SAMPLE_RATE * 30, restrict_to)
        return lang, prob, {"windows": 0, "votes": {}}

    agg: Dict[str, float] = {}
    votes: Dict[str, int] = {}
    for start, end in windows:
        lang, _prob, probs = _detect_language_on_chunk(model, audio, start, end, restrict_to)
        if not lang:
            continue
        for l, p in probs.items():
            agg[l] = agg.get(l, 0.0) + p
        votes[lang] = votes.get(lang, 0) + 1

    if not agg:
        return None, 0.0, {"windows": len(windows), "votes": votes}

    total = sum(agg.values())
    averaged = {l: p / total for l, p in agg.items()}
    best = max(averaged.items(), key=lambda kv: kv[1])
    details = {"windows": len(windows), "votes": votes, "averaged_probs": averaged}
    return best[0], float(best[1]), details


# ---------------------------------------------------------------------------
# Stage 3 helpers - per-segment code-switch rescue
# ---------------------------------------------------------------------------
def _segment_is_suspicious(seg: Any) -> bool:
    """Heuristics that flag a segment likely decoded in the WRONG language."""
    if seg.no_speech_prob is not None and seg.no_speech_prob > 0.6:
        return False  # probably not speech at all; don't waste a rescue
    if seg.temperature is not None and seg.temperature > 0.0:
        return True  # decoding needed temperature fallback = low confidence
    if seg.avg_logprob is not None and seg.avg_logprob < -0.7:
        return True
    if seg.compression_ratio is not None and seg.compression_ratio > 2.4:
        return True  # repetitive/hallucinated text
    return False


def _rescue_segment(
    model: Any,
    audio: np.ndarray,
    seg: Any,
    seg_dict: Dict[str, Any],
    current_lang: str,
    restrict_to: Optional[Tuple[str, ...]],
) -> Optional[Dict[str, Any]]:
    """
    Re-checks one low-confidence segment: detects the language of its audio
    slice and, if it differs from the language used to decode it, re-transcribes
    the slice with the correct language. Returns an updated segment dict or None.
    """
    pad = int(0.75 * SAMPLE_RATE)
    start_s = int(seg.start * SAMPLE_RATE)
    end_s = int(seg.end * SAMPLE_RATE)
    slice_start = max(0, start_s - pad)
    slice_end = min(len(audio), end_s + pad)
    chunk = audio[slice_start:slice_end]
    if chunk.size < SAMPLE_RATE:  # < 1s, not worth rescuing
        return None

    rescue_lang, prob, _ = _detect_language_on_chunk(
        model, chunk, 0, chunk.size, restrict_to
    )
    if not rescue_lang or rescue_lang == current_lang or prob < 0.6:
        return None

    try:
        rescue_segments, _info = model.transcribe(
            chunk,
            language=rescue_lang,
            task="transcribe",
            beam_size=5,
            best_of=5,
            temperature=[0.0, 0.2, 0.4],
            condition_on_previous_text=False,
            word_timestamps=True,
            vad_filter=False,
        )
    except Exception:
        return None

    offset = slice_start / SAMPLE_RATE
    new_words: List[Dict[str, Any]] = []
    new_texts: List[str] = []
    for rseg in rescue_segments:
        text = rseg.text.strip()
        if not text:
            continue
        new_texts.append(text)
        if rseg.words:
            for w in rseg.words:
                word_text = w.word.strip()
                if not word_text:
                    continue
                new_words.append(
                    {
                        "start": round(offset + w.start, 3),
                        "end": round(offset + w.end, 3),
                        "word": word_text,
                        "probability": round(w.probability, 3),
                    }
                )

    if not new_texts:
        return None

    seg_dict["text"] = " ".join(new_texts)
    seg_dict["language"] = rescue_lang
    if new_words:
        seg_dict["words"] = new_words
    return seg_dict


# ---------------------------------------------------------------------------
# Main transcription entry-point
# ---------------------------------------------------------------------------
def transcribe_audio(
    video_path: str,
    model_size: str = DEFAULT_WHISPER_MODEL,
    language: Optional[str] = None,
    total_duration: float = 0.0,
    progress_callback: Optional[Callable[[float, str], None]] = None,
) -> Dict[str, Any]:
    """
    Transcribes audio from *video_path* with word-level timestamps and
    robust Portuguese/Spanish language handling.

    Parameters
    ----------
    video_path        : path to any media file ffmpeg can read
    model_size        : Whisper model variant (default now "large-v3")
    language          : None/"auto" -> smart multi-region detection + rescue
                        "pt-es"      -> smart detection restricted to pt/es
                        "pt"/"es"/.. -> force this language for every segment
    total_duration    : used only for progress estimation
    progress_callback : callable (percent: float, message: str) -> None

    Returns
    -------
    dict: language, language_probability, full_text, segments, words
          (each segment also carries its own "language" key)
    """
    from faster_whisper.audio import decode_audio

    if progress_callback:
        progress_callback(
            8.0,
            f"Inicializando modelo de transcricao Whisper ({model_size}) na RTX...",
        )

    model = get_whisper_model(model_size)

    # ------------------------------------------------------------------
    # Decode audio once - reused by LID, transcription and rescues.
    # ------------------------------------------------------------------
    audio = decode_audio(video_path, sampling_rate=SAMPLE_RATE)

    # ------------------------------------------------------------------
    # Normalise the language argument
    # ------------------------------------------------------------------
    lang_arg = (language or "").strip().lower() or None
    pinned: Optional[str] = None
    restrict_to: Optional[Tuple[str, ...]] = None
    if lang_arg and lang_arg not in ("auto",):
        if lang_arg in BILINGUAL_PAIRS:
            restrict_to = BILINGUAL_PAIRS[lang_arg]
        else:
            pinned = lang_arg

    # ------------------------------------------------------------------
    # Stage 1 - multi-region language identification
    # ------------------------------------------------------------------
    if progress_callback:
        progress_callback(14.0, "Detectando idioma em multiplas regioes do audio...")

    detected_language: Optional[str] = None
    detected_prob = 0.0
    lid_details: Dict[str, Any] = {}

    if pinned:
        detected_language, detected_prob = pinned, 1.0
        lid_details = {"mode": "pinned"}
    else:
        detected_language, detected_prob, lid_details = detect_dominant_language(
            model, audio, restrict_to=restrict_to
        )
        if not detected_language:
            detected_language, detected_prob = "pt", 0.0

    decode_lang = pinned or detected_language

    if progress_callback:
        pct_txt = f"{detected_prob:.0%}" if detected_prob else "?"
        progress_callback(
            18.0,
            f"Idioma detectado: {decode_lang} ({pct_txt}). Transcrevendo fala...",
        )

    # ------------------------------------------------------------------
    # Stage 2 - full transcription with the dominant language pinned.
    # condition_on_previous_text=False kills the cross-window prompt
    # contamination that silently "translated" Spanish into Portuguese.
    # ------------------------------------------------------------------
    segments_gen, info = model.transcribe(
        audio,
        language=decode_lang,
        task="transcribe",
        multilingual=False,
        beam_size=5,
        best_of=5,
        temperature=[0.0, 0.2, 0.4, 0.6, 0.8, 1.0],
        no_speech_threshold=0.55,
        log_prob_threshold=-1.2,
        compression_ratio_threshold=2.6,
        condition_on_previous_text=False,
        word_timestamps=True,
        vad_filter=True,
        vad_parameters=dict(
            min_silence_duration_ms=300,
            threshold=0.45,
            speech_pad_ms=400,
        ),
        hallucination_silence_threshold=2.0,
    )

    all_segments: List[Dict[str, Any]] = []
    all_words: List[Dict[str, Any]] = []
    full_transcript: List[str] = []
    suspicious: List[Tuple[Any, Dict[str, Any]]] = []

    for seg in segments_gen:
        text = seg.text.strip()
        if not text:
            continue

        seg_dict: Dict[str, Any] = {
            "start": round(seg.start, 3),
            "end": round(seg.end, 3),
            "text": text,
            "language": decode_lang,
            "words": [],
        }
        full_transcript.append(text)

        if seg.words:
            for w in seg.words:
                word_text = w.word.strip()
                if not word_text:
                    continue
                w_dict = {
                    "start": round(w.start, 3),
                    "end": round(w.end, 3),
                    "word": word_text,
                    "probability": round(w.probability, 3),
                }
                seg_dict["words"].append(w_dict)
                all_words.append(w_dict)
        else:
            # Fallback robusto: se Whisper nao retornar timestamps por palavra,
            # distribui proporcionalmente as palavras do segmento.
            raw_words = text.split()
            if raw_words:
                seg_dur = max(0.1, seg.end - seg.start)
                w_dur = seg_dur / len(raw_words)
                for idx, rw in enumerate(raw_words):
                    w_dict = {
                        "start": round(seg.start + idx * w_dur, 3),
                        "end": round(seg.start + (idx + 1) * w_dur, 3),
                        "word": rw,
                        "probability": 0.9,
                    }
                    seg_dict["words"].append(w_dict)
                    all_words.append(w_dict)

        all_segments.append(seg_dict)

        if not pinned and _segment_is_suspicious(seg):
            suspicious.append((seg, seg_dict))

        if progress_callback and total_duration > 0:
            pct = min(82.0, 18.0 + (seg.end / total_duration) * 64.0)
            progress_callback(
                pct,
                f"Transcrevendo: {seg.end:.1f}s de {total_duration:.1f}s ({text[:35]}...)",
            )

    # ------------------------------------------------------------------
    # Stage 3 - code-switch rescue on low-confidence segments.
    # ------------------------------------------------------------------
    rescued = 0
    if suspicious:
        if progress_callback:
            progress_callback(
                84.0,
                f"Verificando {len(suspicious)} trechos de baixa confianca (troca de idioma)...",
            )
        max_rescues = max(20, int(0.4 * len(all_segments)))
        for seg, seg_dict in suspicious[:max_rescues]:
            updated = _rescue_segment(
                model, audio, seg, seg_dict, decode_lang, restrict_to
            )
            if updated is not None:
                rescued += 1

        if rescued:
            all_words = []
            full_transcript = []
            for seg_dict in all_segments:
                full_transcript.append(seg_dict["text"])
                all_words.extend(seg_dict["words"])

        if progress_callback:
            progress_callback(
                86.0,
                f"Verificacao concluida: {rescued} trecho(s) corrigido(s) para o idioma correto.",
            )

    return {
        "language": detected_language,
        "language_probability": round(detected_prob, 3),
        "full_text": " ".join(full_transcript),
        "segments": all_segments,
        "words": all_words,
        "language_details": lid_details,
        "rescued_segments": rescued,
    }
