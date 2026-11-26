"""URL parsing, metadata extraction and format selection."""

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlsplit

from . import updater as up

log = logging.getLogger("youtube")

VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")
YTDLP_ARGS = ("yt-dlp", "--ignore-config", "--no-playlist", "--no-warnings", "--no-progress")
MAX_PREFETCH = 4
INFO_TTL = 3600  # media URLs stay valid for about six hours


class VideoError(Exception):
    """A problem worth showing to the viewer."""


def parse_video_id(text: str) -> str | None:
    """Accepts a bare ID or any common YouTube/Invidious/Piped URL."""
    text = text.strip()
    if VIDEO_ID.fullmatch(text):
        return text
    try:
        url = urlsplit(text if "://" in text else f"https://{text}")
    except ValueError:
        return None
    query_id = parse_qs(url.query).get("v", [""])[0]
    if VIDEO_ID.fullmatch(query_id):
        return query_id
    parts = [p for p in url.path.split("/") if p]
    if len(parts) >= 2 and parts[0] in ("embed", "shorts", "live", "v", "e"):
        candidate = parts[1]
    elif len(parts) == 1 and (url.hostname or "").lower() in ("youtu.be", "www.youtu.be"):
        candidate = parts[0]
    else:
        return None
    return candidate if VIDEO_ID.fullmatch(candidate) else None


def quality_of(fmt: dict) -> int | None:
    """The "1080p" style label. It is not the height for vertical or wide videos."""
    if m := re.match(r"(\d+)p", fmt.get("format_note") or ""):
        return int(m[1])
    sides = [s for s in (fmt.get("width"), fmt.get("height")) if s]
    return min(sides) if sides else None


def _has_video(f: dict) -> bool:
    return (f.get("vcodec") or "none") != "none"


def _has_audio(f: dict) -> bool:
    return (f.get("acodec") or "none") != "none"


def _usable(f: dict) -> bool:
    return not f.get("has_drm") and (
        f.get("protocol") in ("http", "https") or str(f.get("protocol")).startswith("m3u8")
    )


def _direct(f: dict) -> bool:
    return f.get("protocol") in ("http", "https")


def _video_rank(f: dict, quality: int) -> tuple:
    codec = f["vcodec"]
    h264, vp9 = codec.startswith("avc1"), codec.startswith(("vp9", "vp09"))
    # H.264 plays everywhere, but YouTube only offers it up to 1080p.
    codec_rank = (3 if h264 else 2 if vp9 else 1) if quality <= 1080 else (3 if vp9 else 2)
    sdr = (f.get("dynamic_range") or "SDR") == "SDR"
    return (sdr, codec_rank, _direct(f), f.get("fps") or 0, f.get("tbr") or 0)


def _audio_rank(f: dict) -> tuple:
    return (
        f.get("language_preference") or -1,  # the original track over dubs
        f["acodec"].startswith("mp4a"),
        (f.get("audio_channels") or 2) <= 2,
        "drc" not in f["format_id"],
        _direct(f),
        f.get("abr") or f.get("tbr") or 0,
    )


@dataclass
class Choice:
    formats: list[str]
    size: int
    available_at: float = 0

    def wait(self) -> float:
        """Seconds until YouTube serves these streams. Videos with ads have a delay."""
        return max(0.0, self.available_at - time.time())


@dataclass
class Info:
    raw: bytes
    meta: dict
    choices: dict[int, Choice]
    fetched: float = field(default_factory=time.monotonic)

    def pick(self, wanted: int) -> int:
        """Best available quality not above `wanted`, else the lowest there is."""
        below = [q for q in self.choices if q <= wanted]
        return max(below) if below else min(self.choices)


def choose_formats(formats: list[dict], duration: float) -> dict[int, Choice]:
    def size(f: dict) -> int:
        return int(
            f.get("filesize")
            or f.get("filesize_approx")
            or (f.get("tbr") or 0) * 1000 / 8 * duration
        )

    usable = [f for f in formats if _usable(f)]
    audio = [f for f in usable if _has_audio(f) and not _has_video(f)]
    best_audio = max(audio, key=_audio_rank, default=None)
    by_quality: dict[int, list[dict]] = {}
    for f in usable:
        if _has_video(f) and (q := quality_of(f)):
            by_quality.setdefault(q, []).append(f)

    choices = {}
    for q, options in sorted(by_quality.items()):
        separate = [f for f in options if not _has_audio(f)]
        combined = [f for f in options if _has_audio(f)]
        if separate and (best_audio or not combined):
            video = max(separate, key=lambda f: _video_rank(f, q))
            picked = [video, best_audio] if best_audio else [video]
        else:
            picked = [max(combined, key=lambda f: _video_rank(f, q))]
        choices[q] = Choice(
            [f["format_id"] for f in picked],
            sum(map(size, picked)),
            max(f.get("available_at") or 0 for f in picked),
        )
    return choices


def parse_info(raw: bytes) -> Info:
    d = json.loads(raw)
    if d.get("live_status") in ("is_live", "is_upcoming"):
        raise VideoError("This is a livestream. Only finished videos can be played here.")
    if d.get("live_status") == "post_live":
        raise VideoError("This livestream just ended and YouTube is still processing it. Try again later.")
    choices = choose_formats(d.get("formats") or [], d.get("duration") or 0)
    if not choices:
        raise VideoError("YouTube offered no playable formats for this video.")
    date = d.get("upload_date") or ""
    meta = {
        "id": d["id"],
        "title": d.get("title") or d["id"],
        "channel": d.get("channel") or d.get("uploader") or "",
        "description": d.get("description") or "",
        "duration": d.get("duration") or 0,
        "views": d.get("view_count"),
        "date": f"{date[:4]}-{date[4:6]}-{date[6:]}" if len(date) == 8 else None,
        "qualities": sorted(choices, reverse=True),
    }
    return Info(raw, meta, choices)


def error_message(stderr: str) -> str:
    lines = [ln for ln in stderr.splitlines() if ln.startswith("ERROR:")]
    if not lines:
        return "yt-dlp could not read this video."
    message = re.sub(r"^ERROR:\s*(\[[^\]]+\]\s*)?([A-Za-z0-9_-]{11}:\s*)?", "", lines[-1])
    # Advice aimed at people running yt-dlp by hand.
    message = re.split(r"\s*(?:Use --|; please report|Please report|See\s+https?:)", message)[0]
    return message.strip() or "yt-dlp could not read this video."


class Library:
    """Caches metadata so that changing quality does not mean asking YouTube again."""

    def __init__(self, updater: up.Updater):
        self.updater = updater
        self._tasks: dict[str, asyncio.Task[Info]] = {}

    def forget(self, video_id: str) -> None:
        self._tasks.pop(video_id, None)

    def prefetch(self, video_id: str) -> None:
        """Start reading a video that is about to be opened."""
        if sum(not t.done() for t in self._tasks.values()) < MAX_PREFETCH:
            self._start(video_id)

    async def info(self, video_id: str) -> Info:
        # Shielded: one viewer leaving must not cancel it for another.
        return await asyncio.shield(self._start(video_id))

    def _start(self, video_id: str) -> asyncio.Task[Info]:
        for key, task in list(self._tasks.items()):
            if task.done() and (
                task.cancelled()
                or task.exception()
                or time.monotonic() - task.result().fetched > INFO_TTL
            ):
                del self._tasks[key]
        task = self._tasks.get(video_id)
        if task is None:
            task = self._tasks[video_id] = asyncio.create_task(self._fetch(video_id))
            # Marks a failure as seen, in case the viewer leaves before it happens.
            task.add_done_callback(lambda t: t.cancelled() or t.exception())
        return task

    async def _fetch(self, video_id: str) -> Info:
        await asyncio.wait_for(self.updater.ready.wait(), 180)
        try:
            return await self._extract(video_id)
        except VideoError as e:
            # YouTube may have changed something that a newer yt-dlp handles.
            if await self.updater.check(min_interval=300):
                log.info("retrying %s with the updated yt-dlp after: %s", video_id, e)
                return await self._extract(video_id)
            raise

    async def _extract(self, video_id: str) -> Info:
        started = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            *YTDLP_ARGS, "-J", "--", f"https://www.youtube.com/watch?v={video_id}",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=up.environment(),
        )  # fmt: skip
        try:
            out, err = await asyncio.wait_for(proc.communicate(), 120)
        except TimeoutError:
            raise VideoError("YouTube took too long to answer.") from None
        finally:
            if proc.returncode is None:
                proc.kill()
        if proc.returncode:
            stderr = err.decode(errors="replace")
            log.warning("extraction of %s failed: %s", video_id, stderr.strip()[-500:])
            raise VideoError(error_message(stderr))
        info = parse_info(out)
        log.info("extracted %s in %.1fs", video_id, time.monotonic() - started)
        return info
