# AutoClip-AI 🎬⚡

Gere **Shorts, Reels e TikToks** automaticamente a partir de vídeos longos, **100% local** e acelerado pela sua GPU — uma alternativa self-hosted e privada ao OpusClip.

---

## 🚀 O que ele faz

### Modo Cortes Virais

1. **Download Automático**: Baixa o vídeo a partir de URLs (YouTube, TikTok, Instagram, Twitch e mais de 1000 plataformas via `yt-dlp`) ou aceita upload de arquivo local (`.mp4`, `.mov`, `.mkv`, etc.).
2. **Transcrição de Alta Precisão**: Transcreve a fala com `faster-whisper` com marcação de tempo (timestamps) palavra por palavra acelerado por CUDA / GPU.
3. **Compreensão de Pedido (Prompt)**: Permite orientar o corte em linguagem natural (ex: *"momentos engraçados"*, *"as melhores dicas"*, *"jogadas épicas"*) com filtragem por gênero e contexto.
4. **Análise de Áudio e Imagem**: Mede energia e variação da fala, detecta trocas de cenas com `ffmpeg` e `numpy`, combinando métricas de retenção sem precisar de modelos de visão excessivamente pesados.
5. **Score de Viralidade Transparente**: Avalia cada trecho com pontuação detalhada: gancho, palavras-chave, ritmo, retenção, emoção, dinâmica de áudio/imagem e aderência ao pedido do usuário.
6. **Reenquadramento Inteligente (9:16, 1:1, 16:9)**: Rastreamento facial em tempo real com MediaPipe BlazeFace (com fallback para Haar Cascade ou recorte centralizado).
7. **Auto-Edit**: Remove pausas mortas e silêncios excessivos preservando o raciocínio e a cadência do orador.
8. **Legendas Animadas Dinâmicas**: Legendas estilo Hormozi / MrBeast em formato `.ass`, destaque palavra por palavra, gancho nos primeiros segundos, chamada para ação (CTA) ao final e suporte a emojis contextuais.
9. **Renderização Acelerada por Hardware**: Exportação ultra-rápida via `ffmpeg` com encoder NVIDIA NVENC (`h264_nvenc`) com fallback inteligente para CPU (`libx264`).
10. **Kit de Publicação Pronto**: Gera automaticamente título chamativo, gancho, descrição e hashtags relevantes para copiar e colar diretamente nas redes.

Tudo gerenciado através de uma **Interface Web moderna**, com progresso em tempo real via WebSocket, histórico de jobs e download dos clipes prontos.

---

## 🛠️ Requisitos do Sistema

- **Sistema Operacional**: Windows 10/11 (compatível também com Linux e macOS)
- **Python**: 3.10+
- **FFmpeg**: Instalado e adicionado ao `PATH` do sistema (com suporte a `libass` e idealmente NVENC)
- **Placa de Vídeo**: NVIDIA GeForce com suporte CUDA (testado na RTX Série 30/40/50). *Funciona em CPU caso não haja GPU dedicada.*
- **Espaço em Disco**: ~2 GB livres para cache de modelos (Whisper e BlazeFace).

---

## 📦 Instalação

### 1. Clonar o repositório
```bash
git clone https://github.com/Alexxpinheiroo/AutoClip-AI.git
cd AutoClip-AI
```

### 2. Criar e ativar o ambiente virtual
```bash
python -m venv .venv
.venv\Scripts\activate
```

### 3. Instalar o PyTorch com suporte CUDA
```bash
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu128
```
*(Se não possuir GPU NVIDIA, instale o PyTorch padrão: `pip install torch torchvision torchaudio`)*

### 4. Instalar as dependências do projeto
```bash
pip install -r requirements.txt
```

---

## ⚡ Como Usar

### 🖥️ Pela Interface Web (Recomendado)
Basta dar dois cliques no arquivo:
```bat
start.bat
```
Ou rodar pelo terminal:
```bash
python run.py
```
A aplicação iniciará o servidor e abrirá o navegador em: **`http://127.0.0.1:5050`**

#### Opções de inicialização:
```bash
python run.py --no-browser    # Inicia sem abrir o navegador automaticamente
python run.py --port 8080     # Especifica uma porta diferente
python run.py --reload        # Modo recarregamento automático (desenvolvimento)
```

---

### 💻 Pela Linha de Comando (CLI)

```bash
# Gerar 3 cortes a partir de uma URL do YouTube
python -m backend.cli "https://www.youtube.com/watch?v=..." --clips 3

# Processar um arquivo local definindo duração mínima e máxima dos clipes
python -m backend.cli "C:\videos\podcast.mp4" --min 20 --max 60

# Processar com pedido específico e enquadramento vertical (Shorts/TikTok)
python -m backend.cli "C:\videos\podcast.mp4" --prompt "as melhores dicas de treino" --genre fitness --aspect 9:16 --clips 3
```

#### Principais Parâmetros da CLI:
| Flag | Descrição |
| --- | --- |
| `--prompt "..."` | Instrução em linguagem natural para guiar a seleção dos cortes |
| `--genre` | Gênero do conteúdo (`viral`, `humor`, `educacional`, `fitness`, `games`, etc.) |
| `--aspect` | Formato do vídeo: `9:16` (Shorts), `1:1` (Feed) ou `16:9` (Horizontal) |
| `--clips` | Quantidade de clipes desejada |
| `--min` / `--max` | Duração mínima e máxima de cada corte em segundos |
| `--no-silence-removal` | Mantém os silêncios originais sem aplicar o auto-edit |
| `--no-hook` | Não exibe o gancho no início do clipe |
| `--no-cta` | Não exibe o CTA ao final do clipe |
| `--no-emojis` | Desativa a inserção de emojis nas legendas |
| `--no-gpu` | Força a renderização por CPU (`libx264`) em vez de NVENC |
| `--ollama` | Utiliza Ollama (IA local) para ranking semântico avançado |

---

## 🧠 Integração com Ollama (Opcional)

Se desejar potencializar a seleção de trechos com LLM local:
```bash
ollama pull llama3
ollama serve
```
Ative a opção **Ollama (IA Local)** na interface gráfica ou utilize a flag `--ollama` na CLI. Caso o Ollama não esteja em execução, o motor heurístico local assume automaticamente sem interromper o fluxo.

---

## 📂 Estrutura do Projeto

```
AutoClip-AI/
├── backend/
│   ├── analyzer.py            # Ranking heurístico, análise de prompt e viral score
│   ├── cli.py                 # Interface de linha de comando
│   ├── config.py              # Configurações de diretórios, GPU e formatos
│   ├── downloader.py          # Download via yt-dlp e manipulação de arquivos
│   ├── errors.py              # Tratamento centralizado de exceções
│   ├── face_tracker.py        # Rastreamento facial (MediaPipe BlazeFace) e crop
│   ├── media_signals.py       # Extração de energia sonora e detecção de cenas
│   ├── pipeline.py            # Orquestrador do fluxo completo de ponta a ponta
│   ├── server.py              # Servidor API FastAPI e conexões WebSocket
│   ├── subtitle_generator.py  # Renderização de legendas .ass e estilos
│   ├── transcriber.py         # Transcrição via faster-whisper com timestamps
│   └── video_renderer.py      # Renderização final com FFmpeg + NVENC
├── frontend/                  # Interface web (HTML5, CSS3, JavaScript puro)
├── downloads/                 # Cache de downloads temporários
├── outputs/                   # Vídeos finais gerados, legendas e metadados
├── models/                    # Modelos de IA locais (Whisper e BlazeFace)
├── run.py                     # Entrypoint de inicialização da aplicação
├── start.bat                  # Inicializador rápido com 2 cliques para Windows
└── requirements.txt           # Dependências do projeto
```

---

## 📄 Licença e Uso

Uso pessoal e educacional. Ao baixar vídeos da internet, respeite os direitos autorais e termos de uso das plataformas de origem.
