"""AutoClip-AI launcher.

Uso:
    python run.py                 # inicia servidor e abre o navegador
    python run.py --no-browser    # apenas o servidor
    python run.py --port 8080     # porta personalizada
    python run.py --reload        # modo desenvolvimento
"""
import argparse
import threading
import webbrowser

import uvicorn

from backend.config import HOST, PORT


def main() -> None:
    parser = argparse.ArgumentParser(description="AutoClip-AI — cortes virais locais")
    parser.add_argument("--host", default=HOST, help="Endereço do servidor (padrão: %(default)s)")
    parser.add_argument("--port", type=int, default=PORT, help="Porta (padrão: %(default)s)")
    parser.add_argument("--no-browser", action="store_true", help="Não abrir o navegador")
    parser.add_argument("--reload", action="store_true", help="Recarregar ao editar o código")
    args = parser.parse_args()

    url = f"http://{args.host}:{args.port}"
    print(f"\n[*] AutoClip-AI iniciando em {url}\n")

    if not args.no_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()

    uvicorn.run(
        "backend.server:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
    )


if __name__ == "__main__":
    main()
