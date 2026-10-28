import os
import threading
from typing import Dict, Any, List, Callable, Optional
from .config import DEVICE, COMPUTE_TYPE, DEFAULT_WHISPER_MODEL
from faster_whisper import WhisperModel

_model_cache: Dict[str, WhisperModel] = {}
_model_lock = threading.Lock()

def get_whisper_model(model_size: str = DEFAULT_WHISPER_MODEL) -> WhisperModel:
    """
    Caches and returns the WhisperModel instance loaded onto the RTX GPU.
    """
    if model_size not in _model_cache:
        with _model_lock:
            if model_size not in _model_cache:
                print(f"[*] Carregando modelo Whisper '{model_size}' no dispositivo: {DEVICE} ({COMPUTE_TYPE})...")
                _model_cache[model_size] = WhisperModel(
                    model_size,
                    device=DEVICE,
                    compute_type=COMPUTE_TYPE,
                    download_root=os.path.join(os.path.dirname(__file__), "..", "models")
                )
    return _model_cache[model_size]

def transcribe_audio(
    video_path: str,
    model_size: str = DEFAULT_WHISPER_MODEL,
    language: Optional[str] = None,
    total_duration: float = 0.0,
    progress_callback: Optional[Callable[[float, str], None]] = None
) -> Dict[str, Any]:
    """
    Transcribes audio directly from the video file using faster-whisper.
    Extracts word-level timestamps for animated karaoke captions.
    """
    if progress_callback:
        progress_callback(10.0, f"Inicializando modelo de transcrição Whisper ({model_size}) na RTX...")

    model = get_whisper_model(model_size)

    if progress_callback:
        progress_callback(20.0, "Transcrevendo fala e sincronizando palavras...")

    # Transcribe with word timestamps
    segments, info = model.transcribe(
        video_path,
        beam_size=5,
        word_timestamps=True,
        language=language,
        vad_filter=True,
        vad_parameters=dict(min_silence_duration_ms=500)
    )

    detected_language = info.language
    detected_prob = info.language_probability

    all_segments: List[Dict[str, Any]] = []
    all_words: List[Dict[str, Any]] = []
    full_transcript = []

    for seg in segments:
        seg_dict = {
            "start": round(seg.start, 3),
            "end": round(seg.end, 3),
            "text": seg.text.strip(),
            "words": []
        }
        
        full_transcript.append(seg.text.strip())

        if seg.words:
            for w in seg.words:
                w_dict = {
                    "start": round(w.start, 3),
                    "end": round(w.end, 3),
                    "word": w.word.strip(),
                    "probability": round(w.probability, 3)
                }
                seg_dict["words"].append(w_dict)
                all_words.append(w_dict)

        all_segments.append(seg_dict)

        # Estimate progress based on current audio time
        if progress_callback and total_duration > 0:
            current_pct = min(85.0, 20.0 + (seg.end / total_duration) * 65.0)
            progress_callback(current_pct, f"Transcrevendo: {seg.end:.1f}s de {total_duration:.1f}s ({seg.text.strip()[:35]}...)")

    return {
        "language": detected_language,
        "language_probability": round(detected_prob, 3),
        "full_text": " ".join(full_transcript),
        "segments": all_segments,
        "words": all_words
    }
