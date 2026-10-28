"""Processamento via linha de comando.

Exemplos:
    python -m backend.cli "https://www.youtube.com/watch?v=..."
    python -m backend.cli "C:\\videos\\podcast.mp4" --clips 3 --min 20 --max 60
"""
import argparse
import sys
import time
import uuid
from pathlib import Path

from .config import OUTPUTS_DIR
from .pipeline import PipelineOptions, process_job
from .video_renderer import get_video_duration


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AutoClip-AI — cortes virais (CLI)")
    parser.add_argument("source", help="URL do vídeo ou caminho de um arquivo local")
    parser.add_argument("--clips", type=int, default=3, help="número de cortes (padrão: 3)")
    parser.add_argument("--min", type=float, default=20.0, help="duração mínima em segundos")
    parser.add_argument("--max", type=float, default=60.0, help="duração máxima em segundos")
    parser.add_argument("--model", default="base", help="modelo Whisper (tiny/base/small/medium/large-v3)")
    parser.add_argument("--language", default=None, help="idioma (ex.: pt, en). Padrão: auto")
    parser.add_argument("--style", default="hormozi_yellow", help="estilo da legenda")
    parser.add_argument("--ollama", action="store_true", help="usar Ollama para escolher os cortes")
    parser.add_argument("--no-gpu", action="store_true", help="forçar renderização por CPU (libx264)")
    parser.add_argument("--no-face-tracking", action="store_true", help="desativar rastreamento facial")
    parser.add_argument("--prompt", default="", help='pedido em linguagem natural (ex.: "momentos engraçados")')
    parser.add_argument("--genre", default="auto", help="gênero: auto/viral/humor/educacional/entrevista/financas/fitness/games/noticias/esportes")
    parser.add_argument("--aspect", default="9:16", choices=["9:16", "1:1", "16:9"], help="formato do corte")
    parser.add_argument("--no-silence-removal", action="store_true", help="não remover pausas longas (auto-edit)")
    parser.add_argument("--no-hook", action="store_true", help="não exibir gancho no início")
    parser.add_argument("--no-cta", action="store_true", help="não exibir CTA no final")
    parser.add_argument("--no-emojis", action="store_true", help="não usar emojis nas legendas")
    parser.add_argument("--no-multimodal", action="store_true", help="não analisar áudio/cenas")
    return parser


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    args = build_parser().parse_args()
    is_url = args.source.lower().startswith(("http://", "https://"))
    job_id = uuid.uuid4().hex[:10]

    video_meta = None
    if not is_url:
        path = Path(args.source).expanduser().resolve()
        if not path.exists():
            print(f"[!] Arquivo não encontrado: {path}")
            sys.exit(1)
        duration = get_video_duration(str(path))
        video_meta = {
            "video_id": job_id,
            "filepath": str(path),
            "title": path.stem,
            "duration": round(duration, 2),
            "thumbnail": "",
        }

    options = PipelineOptions.from_dict({
        "num_clips": args.clips,
        "min_duration": args.min,
        "max_duration": args.max,
        "whisper_model": args.model,
        "language": args.language,
        "subtitle_style": args.style,
        "use_ollama": args.ollama,
        "use_nvenc": not args.no_gpu,
        "use_face_tracking": not args.no_face_tracking,
        "prompt": args.prompt,
        "genre": args.genre,
        "aspect_ratio": args.aspect,
        "remove_silences": not args.no_silence_removal,
        "show_hook": not args.no_hook,
        "show_cta": not args.no_cta,
        "use_emojis": not args.no_emojis,
        "use_multimodal": not args.no_multimodal,
    })

    last_len = 0

    def update(percent: float, message: str) -> None:
        nonlocal last_len
        line = f"[{percent:5.1f}%] {message}"
        print("\r" + line.ljust(last_len), end="", flush=True)
        last_len = len(line)

    started = time.time()
    try:
        result = process_job(
            job_id,
            options,
            update,
            source_url=args.source if is_url else None,
            video_meta=video_meta,
        )
    except KeyboardInterrupt:
        print("\n[!] Cancelado pelo usuário.")
        sys.exit(130)

    print(f"\n\nConcluído em {result.get('elapsed_seconds', time.time() - started):.1f}s")

    clips = result.get("clips", [])
    if not clips:
        print(result.get("message", "Nenhum corte gerado."))
        return

    print(f"{len(clips)} corte(s) em {OUTPUTS_DIR / job_id}:")
    for clip in clips:
        print(f"  • {clip['filename']}  ({clip['duration']}s)  score={clip['virality_score']}  {clip['title']}")


if __name__ == "__main__":
    main()
