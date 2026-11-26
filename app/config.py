import os
from pathlib import Path


def _env(name: str, default: str) -> str:
    return os.environ.get(name) or default


DATA = Path(_env("DATA_DIR", "/data"))
MEDIA = DATA / "media"
YTDLP_ROOT = DATA / "yt-dlp"
STATE = DATA / "state.json"

PASSWORD = os.environ.get("PASSWORD", "")
DEFAULT_QUALITY = int(_env("DEFAULT_QUALITY", "1080"))
# How long a video stays on disk after the last time somebody played it.
UNPLAYED_SECONDS = float(_env("UNPLAYED_DAYS", "3")) * 86400  # 0 means never
MAX_VIDEOS = int(_env("MAX_VIDEOS", "10"))
MIN_FREE_BYTES = int(float(_env("MIN_FREE_GB", "2")) * 1024**3)
# Host, or host:port, of a VLC whose Remote Playback screen is switched on.
VLC_HOST = os.environ.get("VLC_HOST", "").strip()
YTDLP_CHANNEL = _env("YTDLP_CHANNEL", "nightly")
UPDATE_SECONDS = float(_env("UPDATE_HOURS", "3")) * 3600
# An empty value is meaningful here: it turns SponsorBlock off.
SPONSORBLOCK_CATEGORIES = [
    c.strip()
    for c in os.environ.get("SPONSORBLOCK_CATEGORIES", "sponsor,selfpromo,interaction").split(",")
    if c.strip()
]
