import os
from pathlib import Path

# Reduce noisy third-party logs before heavy imports.
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
os.environ.setdefault("GLOG_minloglevel", "2")
os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "3")

# On Windows, creating symlinks requires Developer Mode/admin. Hugging Face
# otherwise fails with WinError 1314 while caching Whisper models.
if os.name == "nt":
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS", "1")
    os.environ.setdefault("HF_HUB_DISABLE_SHARED_BLOBS", "1")

import torch

BASE_DIR = Path(__file__).resolve().parent.parent
DOWNLOADS_DIR = BASE_DIR / "downloads"
OUTPUTS_DIR = BASE_DIR / "outputs"
MODELS_DIR = BASE_DIR / "models"
FRONTEND_DIR = BASE_DIR / "frontend"
ASSETS_DIR = BASE_DIR / "assets"
STICKERS_DIR = ASSETS_DIR / "stickers"

for directory in [DOWNLOADS_DIR, OUTPUTS_DIR, MODELS_DIR, STICKERS_DIR]:
    directory.mkdir(parents=True, exist_ok=True)

# Hardware settings
DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
COMPUTE_TYPE = "float16" if torch.cuda.is_available() else "int8"
GPU_NAME = torch.cuda.get_device_name(0) if torch.cuda.is_available() else "CPU Mode"

# Defaults
# "large-v3" is the multilingual gold standard (best pt/es accuracy and the
# only reliable option for code-switched content). "base" was the old default
# and was the root cause of wrong-language captions.
DEFAULT_WHISPER_MODEL = "large-v3"  # options: tiny, base, small, medium, large-v3-turbo, large-v3
DEFAULT_CLIP_MIN_DURATION = 20  # seconds
DEFAULT_CLIP_MAX_DURATION = 60  # seconds
TARGET_WIDTH = 1080
TARGET_HEIGHT = 1920  # 9:16 Aspect ratio

# Output formats. Every short-form platform accepts 9:16, but 1:1 and 16:9
# are useful for feeds, LinkedIn and YouTube. Values are (width, height).
ASPECT_RATIOS = {
    "9:16": (1080, 1920),
    "1:1": (1080, 1080),
    "16:9": (1920, 1080),
}
DEFAULT_ASPECT_RATIO = "9:16"

# Video rendering
FFMPEG_BIN = "ffmpeg"
FFPROBE_BIN = "ffprobe"
USE_NVENC = torch.cuda.is_available()  # hardware H.264 encoder when available
VIDEO_CRF = 20
AUDIO_BITRATE = "192k"

# Server
HOST = "127.0.0.1"
PORT = 5050
