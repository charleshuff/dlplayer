"""What SponsorBlock says to leave out, and keeping it where VLC's playlist can.

The browser skips the segments itself: it knows the playhead, it can draw them
on the reel and it can undo a skip. VLC is told a URL and nothing more, so the
only way it skips anything is for the playlist it is handed not to contain it,
which is why the ranges have to reach the server at all.

They are kept beside the video's segments rather than looked up when wanted,
because VLC reloads the playlist of a video that is still downloading, and
nothing in the media path should wait on a third party.
"""

import hashlib
import json
import logging
from pathlib import Path

import aiohttp

from . import config

log = logging.getLogger("sponsorblock")

API = "https://sponsor.ajay.app/api/skipSegments"
SEGMENTS = "sponsor.json"  # the ranges, beside the segments they are cut from


async def lookup(http: aiohttp.ClientSession, video_id: str) -> list[dict]:
    """The ranges to leave out, soonest first. Empty when off or unreachable.

    Asked for by a hash prefix of the id, so SponsorBlock is not told which
    video is being watched here.
    """
    if not config.SPONSORBLOCK_CATEGORIES:
        return []
    prefix = hashlib.sha256(video_id.encode()).hexdigest()[:4]
    params = {"categories": json.dumps(config.SPONSORBLOCK_CATEGORIES), "actionTypes": '["skip"]'}
    found: list[dict] = []
    try:
        async with http.get(f"{API}/{prefix}", params=params) as r:
            if r.status == 200:
                for video in await r.json(content_type=None):
                    if video["videoID"] == video_id:
                        found = [
                            {"start": s["segment"][0], "end": s["segment"][1], "category": s["category"]}
                            for s in video["segments"]
                        ]
    except (aiohttp.ClientError, TimeoutError, ValueError, KeyError) as e:
        log.warning("SponsorBlock lookup failed: %s", e)
    return sorted(found, key=lambda s: s["start"])


def ranges(segments: list[dict]) -> list[tuple[float, float]]:
    """The same thing as (start, end) pairs, overlaps merged.

    Categories overlap -- a sponsor read out over the intro is both -- and a
    segment of video decides whether to go by how much of it is covered, which
    would count that time twice.
    """
    merged: list[list[float]] = []
    for s in sorted(segments, key=lambda s: float(s["start"])):
        start, end = float(s["start"]), float(s["end"])
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(start, end) for start, end in merged]


def remember(directory: Path, segments: list[dict]) -> None:
    """Keep them beside the video, for the playlist VLC asks for again later."""
    try:
        (directory / SEGMENTS).write_text(json.dumps(segments))
    except OSError as e:
        log.warning("could not keep SponsorBlock's answer beside %s: %s", directory.name, e)


def remembered(directory: Path) -> list[tuple[float, float]]:
    """What was kept, ready to cut a playlist with. Nothing kept means none."""
    try:
        return ranges(json.loads((directory / SEGMENTS).read_bytes()))
    except (OSError, ValueError, TypeError, KeyError):
        return []
