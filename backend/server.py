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
    USE_NVENC,
)
from .pipeline import PipelineCancelled, PipelineOptions, process_job
from .subtitle_generator import SUBTITLE_STYLES


@asynccontextmanager
async def lifespan(app_: FastAPI):
    _load_previous_jobs()
    print("=" * 62)
    print("  AutoClip-AI  |  OpusClip local na sua GPU")
    print(f"  Dispositivo: {DEVICE}  |  {GPU_NAME}")
    print(f"  NVENC: {'ativo' if USE_NVENC else 'desativado'}  |  ffmpeg: {shutil.which(FFMPEG_BIN) or 'NÃO ENCONTRADO'}")
    print(f"  Interface: http://{HOST}:{PORT}")
    print("=" * 62)
    yield


app = FastAPI(title="AutoClip-AI", version="1.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

JOBS: Dict[str, "Job"] = {}
VIDEOS: Dict[str, Dict[str, Any]] = {}
JOBS_LOCK = threading.Lock()
# The GPU pipeline is serialized so two jobs don't fight over VRAM.
JOB_SEMAPHORE = threading.Semaphore(1)

WHISPER_MODELS = ["tiny", "base", "small", "medium", "large-v3"]
SUBTITLE_STYLE_NAMES = list(SUBTITLE_STYLES.keys())


@dataclass
class Job:
    id: str
    source_type: str
    status: str = "queued"
    progress: float = 0.0
    message: str = "Aguardando na fila..."
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    video: Optional[Dict[str, Any]] = None
    clips: List[Dict[str, Any]] = field(default_factory=list)
    error: Optional[str] = None
    language: Optional[str] = None
    elapsed_seconds: Optional[float] = None
    output_dir: Optional[str] = None
    options: Dict[str, Any] = field(default_factory=dict)
    source_url: Optional[str] = None
    cancel_event: threading.Event = field(default_factory=threading.Event)

    def public(self, include_clips: bool = True) -> Dict[str, Any]:
        data = {
            "id": self.id,
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
            if data.get("mode") == "edit":
                continue
            job_id = data.get("job_id") or result_file.parent.name

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
                status="completed",
                progress=100.0,
                message=data.get("message", "Resultado carregado do histórico."),
                created_at=result_file.stat().st_mtime,
                updated_at=result_file.stat().st_mtime,
                video=data.get("video"),
                clips=clips,
                language=data.get("language"),
                elapsed_seconds=data.get("elapsed_seconds"),
                output_dir=str(result_file.parent.resolve()),
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
    options = PipelineOptions.from_dict(job.options)
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
                result = process_job(
                    job.id,
                    options,
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

    job = Job(
        id=uuid.uuid4().hex[:10],
        source_type="upload",
        options=payload.options or {},
        video=meta,
    )
    _start_job(job, video_meta=meta)
    return {"job_id": job.id, "video": meta}


@app.post("/api/process/url")
def process_url(payload: ProcessUrlRequest) -> Dict[str, Any]:
    url = (payload.url or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="Informe uma URL válida (http/https).")

    job = Job(
        id=uuid.uuid4().hex[:10],
        source_type="url",
        options=payload.options or {},
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
    """Removes a job from the history (does not delete output files)."""
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
    meta = {
        "video_id": video_id,
        "filepath": str(dest.resolve()),
        "title": Path(clean_name).stem,
        "duration": round(duration, 2),
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
        "aspect_ratios": [
            {"id": key, "label": key, "width": value[0], "height": value[1]}
            for key, value in ASPECT_RATIOS.items()
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

if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
else:
    @app.get("/")
    def missing_frontend() -> Dict[str, str]:
        return {"error": "Pasta frontend não encontrada."}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("backend.server:app", host=HOST, port=PORT, reload=False)
