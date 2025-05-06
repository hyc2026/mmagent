import logging
import json
import os

# Load processing config
processing_config = json.load(open("configs/processing_config.json"))
logging_level = processing_config["logging"]
model = processing_config["model"]

# Configure root logger
if processing_config.get("train", False):
    logging.basicConfig(level=logging.CRITICAL)  # Disable all logs in training mode
else:
    logging.basicConfig(
        level=logging.DEBUG if logging_level == "DETAIL" else logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.StreamHandler(),  # Console handler
            logging.FileHandler(os.path.join(processing_config["log_dir"], "mmagent.log"))  # File handler
        ]
    )

# Disable third-party library logging
logging.getLogger('moviepy').setLevel(logging.ERROR)
logging.getLogger('moviepy.video.io.VideoFileClip').setLevel(logging.ERROR)
logging.getLogger('moviepy.audio.io.AudioFileClip').setLevel(logging.ERROR)
logging.getLogger("httpx").setLevel(logging.CRITICAL)
logging.getLogger("urllib3").setLevel(logging.CRITICAL)
logging.getLogger("httpcore").setLevel(logging.CRITICAL)

from . import retrieve
from . import face_processing
from . import memory_processing
try:
    if model == "qwen2.5-omni":
        from . import memory_processing_qwen
except:
    pass
from . import prompts
from . import videograph
from . import voice_processing
from . import utils
from . import idl

__all__ = [
    "retrieve",
    "face_processing",
    "memory_processing",
    "memory_processing_qwen" if model == "qwen2.5-omni" else None,
    "prompts",
    "videograph",
    "voice_processing",
    "utils",
    "idl",
]