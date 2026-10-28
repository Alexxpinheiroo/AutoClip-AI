# -*- coding: utf-8 -*-
import json
import os
import re
import shutil
import threading
import time
import traceback
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, File, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import video_renderer
from .analyzer import GENRE_PRESETS
from .config import (
    ASPECT_RATIOS,
    ASSETS_DIR,
    DEFAULT_ASPECT_RATIO,
    DEFAULT_CLIP_MAX_DURATION,
    DEFAULT_CLIP_MIN_DURATION,
    DEFAULT_WHISPER_MODEL,
    DEVICE,
    DOWNLOADS_DIR,
    FFMPEG_BIN,
    FRONTEND_DIR,
    GPU_NAME,
    HOST,
    OUTPUTS_DIR,
    PORT,
    STICKERS_DIR,
    USE_NVENC,
)
from .pipeline import PipelineCancelled, PipelineOptions, process_job
from .full_video_pipeline import FullVideoOptions, process_full_video_job
from .sticker_generator import STICKER_DEFINITIONS, ensure_stickers
from .subtitle_generator import SUBTITLE_STYLES


@asynccontextmanager
async def lifespan(app_: FastAPI):
    # Ensure default stickers exist
    try:
        ensure_stickers(STICKERS_DIR)
    except Exception as exc:
        print(f"[!] Aviso ao gerar stickers: {exc}")

    _load_previous_jobs()
    print("=" * 62)
    print("  AutoClip-AI  |  Cortes Virais & Legendador com Adesivos")
    print(f"  Dispositivo: {DEVICE}  |  {GPU_NAME}")
    print(f"  NVENC: {'ativo' if USE_NVENC else 'desativado'}  |  ffmpeg: {shutil.which(FFMPEG_BIN) or 'NÃO ENCONTRADO'}")
    print(f"  Interface: http://{HOST}:{PORT}")
    print("=" * 62)
    yield


app = FastAPI(title="AutoClip-AI", version="1.2.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

JOBS: Dict[str, "Job"] = {}
VIDEOS: Dict[str, Dict[str, Any]] = {}
JOBS_LOCK = threading.Lock()
JOB_SEMAPHORE = threading.Semaphore(1)

WHISPER_MODELS = ["tiny", "base", "small", "medium", "large-v3-turbo", "large-v3"]
SUBTITLE_STYLE_NAMES = list(SUBTITLE_STYLES.keys())


@dataclass
class Job:
    id: str
    source_type: str
    status: str = "queued"
    mode: str = "clips"  # clips or full_video
    progress: float = 0.0
    message: str = "Aguardando na fila..."
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    video: Optional[Dict[str, Any]] = None
    clips: List[Dict[str, Any]] = field(default_factory=list)
    output_file: Optional[Dict[str, Any]] = None
    srt_file: Optional[Dict[str, Any]] = None
    stickers_count: int = 0
    stickers_timeline: List[Dict[str, Any]] = field(default_factory=list)
    thumbnail_url: Optional[str] = None
    error: Optional[str] = None
    language: Optional[str] = None
    elapsed_seconds: Optional[float] = None
    output_dir: Optional[str] = None
    options: Dict[str, Any] = field(default_factory=dict)
    source_url: Optional[str] = None
    cancel_event: threading.Event = field(default_factory=threading.Event)
    chapters_file: Optional[Dict[str, Any]] = None
    chapters_json: Optional[Dict[str, Any]] = None
    chapters: List[Dict[str, Any]] = field(default_factory=list)
    youtube_metadata: Optional[Dict[str, Any]] = None
    metadata_file: Optional[Dict[str, Any]] = None

    def public(self, include_clips: bool = True) -> Dict[str, Any]:
        data = {
            "id": self.id,
            "mode": self.mode,
            "source_type": self.source_type,
            "status": self.status,
            "progress": round(self.progress, 1),
            "message": self.message,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "video": self.video,
            "error": self.error,
            "language": self.language,
            "elapsed_seconds": self.elapsed_seconds,
            "output_dir": self.output_dir,
            "output_file": self.output_file,
            "srt_file": self.srt_file,
            "chapters_file": self.chapters_file,
            "chapters_json": self.chapters_json,
            "chapters": self.chapters,
            "youtube_metadata": self.youtube_metadata,
            "metadata_file": self.metadata_file,
            "stickers_count": self.stickers_count,
            "stickers_timeline": self.stickers_timeline,
            "thumbnail_url": self.thumbnail_url,
            "options": self.options,
            "clip_count": len(self.clips),
        }
        if include_clips:
            data["clips"] = self.clips
        return data


def _update_job(job_id: str, **fields: Any) -> None:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return
        for key, value in fields.items():
            setattr(job, key, value)
        job.updated_at = time.time()


def _sanitize_filename(name: str) -> str:
    name = Path(name or "video.mp4").name
    return re.sub(r'[\\/*?:"<>|]', "_", name).strip() or "video.mp4"


def _load_previous_jobs() -> None:
    """Restores completed jobs from outputs/ so results survive a restart."""
    if not OUTPUTS_DIR.exists():
        return
    for result_file in sorted(OUTPUTS_DIR.glob("*/result.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            data = json.loads(result_file.read_text(encoding="utf-8"))
            job_id = data.get("job_id") or result_file.parent.name
            mode = data.get("mode", "clips")

            if mode == "full_video":
                out_file = data.get("output_file")
                srt_file = data.get("srt_file")
                thumb_path = result_file.parent / "thumbnail.jpg"
                thumb_url = f"/outputs/{job_id}/thumbnail.jpg" if thumb_path.exists() else None

                job = Job(
                    id=job_id,
                    source_type="history",
                    mode="full_video",
                    status="completed",
                    progress=100.0,
                    message=data.get("message", "Vídeo completo carregado do histórico."),
                    created_at=result_file.stat().st_mtime,
                    updated_at=result_file.stat().st_mtime,
                    video=data.get("video"),
                    output_file=out_file,
                    srt_file=srt_file,
                    chapters_file=data.get("chapters_file"),
                    chapters_json=data.get("chapters_json"),
                    chapters=data.get("chapters", []),
                    youtube_metadata=data.get("youtube_metadata"),
                    metadata_file=data.get("metadata_file"),
                    stickers_count=data.get("stickers_count", 0),
                    stickers_timeline=data.get("stickers_timeline", []),
                    thumbnail_url=thumb_url,
                    language=data.get("language"),
                    elapsed_seconds=data.get("elapsed_seconds"),
                    output_dir=str(result_file.parent.resolve()),
                    options=data.get("options", {}),
                )
                with JOBS_LOCK:
                    JOBS[job_id] = job
                continue

            # Clips mode
            clips = []
            for clip in data.get("clips", []):
                clip = dict(clip)
                if not clip.get("url") and clip.get("filename"):
                    clip["url"] = f"/outputs/{job_id}/{clip['filename']}"
                thumb_path = result_file.parent / f"clip_{int(clip.get('id', 0)):02d}.jpg"
                if thumb_path.exists():
                    clip["thumbnail_url"] = f"/outputs/{job_id}/{thumb_path.name}"
                else:
                    clip["thumbnail_url"] = clip.get("thumbnail_url") or None
                clips.append(clip)

            job = Job(
                id=job_id,
                source_type="history",
                mode="clips",
                status="completed",
                progress=100.0,
                message=data.get("message", "Cortes carregados do histórico."),
                created_at=result_file.stat().st_mtime,
                updated_at=result_file.stat().st_mtime,
                video=data.get("video"),
                clips=clips,
                language=data.get("language"),
                elapsed_seconds=data.get("elapsed_seconds"),
                output_dir=str(result_file.parent.resolve()),
                options=data.get("settings", {}),
            )
            with JOBS_LOCK:
                JOBS[job_id] = job
        except Exception as exc:
            print(f"[!] Não foi possível carregar {result_file}: {exc}")


# ---------------------------------------------------------------- schemas
class ProcessVideoRequest(BaseModel):
    video_id: str
    options: Optional[Dict[str, Any]] = None


class ProcessUrlRequest(BaseModel):
    url: str
    options: Optional[Dict[str, Any]] = None


# ------------------------------------------------------------------- jobs
def _start_job(job: Job, video_meta: Optional[Dict[str, Any]] = None, source_url: Optional[str] = None) -> None:
    mode = (job.options or {}).get("mode", "clips")
    job.mode = mode

    with JOBS_LOCK:
        JOBS[job.id] = job

    def worker() -> None:
        _update_job(job.id, message="Aguardando na fila...", progress=0.0)

        with JOB_SEMAPHORE:
            if job.cancel_event.is_set():
                _update_job(job.id, status="cancelled", message="Processamento cancelado.")
                return

            _update_job(job.id, status="running", message="Iniciando processamento...", progress=0.5)

            def update(percent: float, message: str) -> None:
                _update_job(job.id, progress=max(0.0, min(100.0, percent)), message=message, status="running")

            try:
                if job.mode == "full_video":
                    # Full Video Subtitles & Timeline Stickers Mode
                    full_options = FullVideoOptions.from_dict(job.options)
                    result = process_full_video_job(
                        job.id,
                        full_options,
                        update,
                        source_url=source_url,
                        video_meta=video_meta,
                        cancel_event=job.cancel_event,
                    )
                    _update_job(
                        job.id,
                        status="completed",
                        progress=100.0,
                        message=result.get("message") or "Vídeo legendado e enriquecido com sucesso!",
                        video=result.get("video"),
                        output_file=result.get("output_file"),
                        srt_file=result.get("srt_file"),
                        chapters_file=result.get("chapters_file"),
                        chapters_json=result.get("chapters_json"),
                        chapters=result.get("chapters", []),
                        youtube_metadata=result.get("youtube_metadata"),
                        metadata_file=result.get("metadata_file"),
                        stickers_count=result.get("stickers_count", 0),
                        stickers_timeline=result.get("stickers_timeline", []),
                        thumbnail_url=result.get("thumbnail_url"),
                        language=result.get("language"),
                        elapsed_seconds=result.get("elapsed_seconds"),
                        output_dir=result.get("output_dir"),
                    )
                else:
                    # OpusClip Clips Mode
                    clip_options = PipelineOptions.from_dict(job.options)
                    result = process_job(
                        job.id,
                        clip_options,
                        update,
                        source_url=source_url,
                        video_meta=video_meta,
                        cancel_event=job.cancel_event,
                    )
                    _update_job(
                        job.id,
                        status="completed",
                        progress=100.0,
                        message=result.get("message") or f"{len(result.get('clips', []))} corte(s) pronto(s)!",
                        video=result.get("video"),
                        clips=result.get("clips", []),
                        language=result.get("language"),
                        elapsed_seconds=result.get("elapsed_seconds"),
                        output_dir=result.get("output_dir"),
                    )

                if result.get("video", {}).get("filepath"):
                    with JOBS_LOCK:
                        VIDEOS[result["video"].get("video_id", job.id)] = result["video"]
            except PipelineCancelled:
                _update_job(job.id, status="cancelled", message="Processamento cancelado.", progress=0.0)
            except Exception as exc:
                traceback.print_exc()
                _update_job(job.id, status="error", message=f"Erro: {exc}", error=str(exc))

    threading.Thread(target=worker, name=f"job-{job.id}", daemon=True).start()


@app.post("/api/process/video")
def process_video(payload: ProcessVideoRequest) -> Dict[str, Any]:
    with JOBS_LOCK:
        meta = VIDEOS.get(payload.video_id)
    if not meta:
        raise HTTPException(status_code=404, detail="Vídeo não encontrado. Envie o arquivo novamente.")

    opts = payload.options or {}
    job = Job(
        id=uuid.uuid4().hex[:10],
        source_type="upload",
        mode=opts.get("mode", "clips"),
        options=opts,
        video=meta,
    )
    _start_job(job, video_meta=meta)
    return {"job_id": job.id, "video": meta}


@app.post("/api/process/url")
def process_url(payload: ProcessUrlRequest) -> Dict[str, Any]:
    url = (payload.url or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="Informe uma URL válida (http/https).")

    opts = payload.options or {}
    job = Job(
        id=uuid.uuid4().hex[:10],
        source_type="url",
        mode=opts.get("mode", "clips"),
        options=opts,
        source_url=url,
    )
    _start_job(job, source_url=url)
    return {"job_id": job.id}


@app.get("/api/jobs")
def list_jobs() -> Dict[str, Any]:
    with JOBS_LOCK:
        jobs = sorted(JOBS.values(), key=lambda j: j.created_at, reverse=True)
        return {"jobs": [j.public(include_clips=False) for j in jobs[:50]]}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> Dict[str, Any]:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job não encontrado.")
        return job.public()


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str) -> Dict[str, Any]:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job não encontrado.")
        job.cancel_event.set()
        if job.status == "queued":
            job.status = "cancelled"
            job.message = "Processamento cancelado."
    return {"ok": True}


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str) -> Dict[str, Any]:
    """Removes a job from the history."""
    with JOBS_LOCK:
        job = JOBS.pop(job_id, None)
    if not job:
        raise HTTPException(status_code=404, detail="Job não encontrado.")
    return {"ok": True}


@app.post("/api/jobs/{job_id}/open-folder")
def open_job_folder(job_id: str) -> Dict[str, Any]:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job não encontrado.")

    folder = Path(job.output_dir) if job.output_dir else OUTPUTS_DIR / job_id
    if not folder.exists():
        raise HTTPException(status_code=404, detail="Pasta de resultados não encontrada.")

    try:
        if os.name == "nt":
            os.startfile(str(folder))  # type: ignore[attr-defined]
        else:
            import subprocess
            opener = "open" if os.uname().sysname == "Darwin" else "xdg-open"
            subprocess.Popen([opener, str(folder)])
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Não foi possível abrir a pasta: {exc}")

    return {"ok": True, "path": str(folder)}


# ----------------------------------------------------------------- uploads
@app.post("/api/upload")
async def upload_video(file: UploadFile = File(...)) -> Dict[str, Any]:
    if not file.filename:
        raise HTTPException(status_code=400, detail="Arquivo inválido.")

    video_id = uuid.uuid4().hex[:8]
    clean_name = _sanitize_filename(file.filename)
    dest = DOWNLOADS_DIR / f"{video_id}_{clean_name}"

    size = 0
    try:
        with dest.open("wb") as handle:
            while True:
                chunk = await file.read(4 * 1024 * 1024)
                if not chunk:
                    break
                handle.write(chunk)
                size += len(chunk)
    except Exception as exc:
        dest.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"Falha ao salvar o arquivo: {exc}")
    finally:
        await file.close()

    if size == 0:
        dest.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="O arquivo enviado está vazio.")

    duration = video_renderer.get_video_duration(str(dest))
    width, height = video_renderer.get_video_dimensions(str(dest))
    meta = {
        "video_id": video_id,
        "filepath": str(dest.resolve()),
        "title": Path(clean_name).stem,
        "duration": round(duration, 2),
        "width": width,
        "height": height,
        "thumbnail": "",
        "size_bytes": size,
    }
    with JOBS_LOCK:
        VIDEOS[video_id] = meta
    return meta


# ---------------------------------------------------------------- metadata
@app.get("/api/hardware")
def hardware() -> Dict[str, Any]:
    return {
        "device": DEVICE,
        "gpu_name": GPU_NAME,
        "cuda": DEVICE == "cuda",
        "nvenc": USE_NVENC,
        "ffmpeg": shutil.which(FFMPEG_BIN) or "",
        "version": app.version,
    }


@app.get("/api/options")
def options() -> Dict[str, Any]:
    return {
        "whisper_models": WHISPER_MODELS,
        "subtitle_styles": SUBTITLE_STYLE_NAMES,
        "subtitle_style_labels": {
            "youtube_clean": "YouTube Clean (Elegante & Confortável)",
            "hormozi_yellow": "Hormozi Yellow (Destaque Amarelo Vibrante)",
            "mrbeast_green": "MrBeast Green (Destaque Verde Neon)",
            "cyber_cyan": "Cyber Cyan (Ciano Futurista)",
            "minimal_white": "Minimal White (Branco Minimalista)",
            "bold_cinema": "Bold Cinema (Estilo Documentário)",
        },
        "aspect_ratios": [
            {"id": "original", "label": "Original (Manter Resolução do Vídeo)"},
            {"id": "16:9", "label": "16:9 Horizontal (YouTube / TV)", "width": 1920, "height": 1080},
            {"id": "9:16", "label": "9:16 Vertical (TikTok / Reels / Shorts)", "width": 1080, "height": 1920},
            {"id": "1:1", "label": "1:1 Quadrado (Feed / Instagram)", "width": 1080, "height": 1080},
        ],
        "genres": [
            {"id": key, "label": value["label"]}
            for key, value in GENRE_PRESETS.items()
        ],
        "prompt_suggestions": [
            "momentos engraçados e improváveis",
            "as dicas mais úteis do vídeo",
            "histórias pessoais marcantes",
            "as melhores jogadas e reações",
            "opiniões polêmicas e debates",
            "explicações rápidas e didáticas",
        ],
        "stickers": {
            "available": [
                {
                    "id": s_id,
                    "label": s_def["label"],
                    "title": s_def["title"],
                    "subtitle": s_def["subtitle"],
                    "preview_url": f"/assets/stickers/{s_def['filename']}",
                }
                for s_id, s_def in STICKER_DEFINITIONS.items()
            ],
            "intervals": [
                {"id": "auto", "label": "Automático (Equilibrado ao longo do vídeo)"},
                {"id": "interval_60", "label": "A cada 1 minuto (60s)"},
                {"id": "interval_120", "label": "A cada 2 minutos (120s)"},
                {"id": "interval_180", "label": "A cada 3 minutos (180s)"},
                {"id": "interval_300", "label": "A cada 5 minutos (300s)"},
                {"id": "custom", "label": "Personalizado (definir segundos)"},
            ],
            "positions": [
                {"id": "bottom_right", "label": "Canto Inferior Direito (Padrão YouTube)"},
                {"id": "bottom_left", "label": "Canto Inferior Esquerdo"},
                {"id": "top_right", "label": "Canto Superior Direito"},
                {"id": "center_bottom", "label": "Centro Inferior (Acima da legenda)"},
            ],
        },
        "subtitle_positions": [
            {"id": "bottom", "label": "Inferior (Padrão)"},
            {"id": "center", "label": "Central"},
            {"id": "top", "label": "Superior"},
        ],
        "defaults": {
            "num_clips": 3,
            "min_duration": DEFAULT_CLIP_MIN_DURATION,
            "max_duration": DEFAULT_CLIP_MAX_DURATION,
            "whisper_model": DEFAULT_WHISPER_MODEL,
            "subtitle_style": "hormozi_yellow",
            "aspect_ratio": DEFAULT_ASPECT_RATIO,
            "genre": "auto",
            "prompt": "",
            "cta_text": "Siga para mais cortes!",
            "use_multimodal": True,
            "remove_silences": True,
            "show_hook": True,
            "show_cta": True,
            "use_emojis": True,
            # Full video defaults
            "full_subtitle_style": "youtube_clean",
            "full_subtitle_position": "bottom",
            "full_aspect_ratio": "original",
            "full_sticker_interval": "auto",
            "full_sticker_position": "bottom_right",
        },
    }


@app.websocket("/ws/jobs/{job_id}")
async def job_websocket(websocket: WebSocket, job_id: str) -> None:
    import asyncio

    await websocket.accept()
    last_payload = None
    try:
        while True:
            with JOBS_LOCK:
                job = JOBS.get(job_id)
                payload = job.public() if job else None
            if payload is None:
                await websocket.send_json({"error": "Job não encontrado."})
                break
            if payload != last_payload:
                await websocket.send_json(payload)
                last_payload = payload
            if payload["status"] in ("completed", "error", "cancelled"):
                break
            await asyncio.sleep(0.35)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        try:
            await websocket.close()
        except Exception:
            pass


# --------------------------------------------------------------- static app
app.mount("/outputs", StaticFiles(directory=str(OUTPUTS_DIR)), name="outputs")
if ASSETS_DIR.exists():
    app.mount("/assets", StaticFiles(directory=str(ASSETS_DIR)), name="assets")

if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
else:
    @app.get("/")
    def missing_frontend() -> Dict[str, str]:
        return {"error": "Pasta frontend não encontrada."}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("backend.server:app", host=HOST, port=PORT, reload=False)
