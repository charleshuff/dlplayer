"""What the server remembers between pages, devices and restarts.

There is no notion of a user here -- one server, one optional password -- so a
position belongs to the video rather than to whoever watched it. That is what
makes the phone pick up where the laptop stopped. The same timestamp decides
how long the video stays on disk, so this is also the retention clock.

Positions outlive the files they point into: a video deleted for age is
downloaded again on the next visit and resumes where it was.
"""

import asyncio
import json
import logging
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from . import config

log = logging.getLogger("store")

MAX_POSITIONS = 2000
# Resuming only matters in the middle. Near either end, start from the top.
MIN_RESUME = 15.0
END_MARGIN = 20.0


@dataclass
class Seen:
    at: float = 0.0  # position in seconds
    duration: float = 0.0
    played: float = 0.0  # wall clock, so it keeps counting while the server is off


class Store:
    def __init__(self, path: Path | None = None):
        self.path = path or config.STATE
        self.seen: dict[str, Seen] = {}
        self._dirty = False
        try:
            saved = json.loads(self.path.read_bytes())["seen"]
            self.seen = {str(k): Seen(**{f: float(v[f]) for f in ("at", "duration", "played")}) for k, v in saved.items()}
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, KeyError) as e:
            log.warning("ignoring unreadable %s: %s", self.path, e)
        log.info("remembering %d position(s)", len(self.seen))

    def touch(self, video_id: str, at: float | None = None, duration: float = 0) -> None:
        """Mark the video as watched now, optionally moving the position."""
        seen = self.seen.setdefault(video_id, Seen())
        if at is not None:
            seen.at = round(at, 1)
        if duration:
            seen.duration = duration
        seen.played = time.time()
        self._dirty = True

    def last_played(self, video_id: str) -> float:
        seen = self.seen.get(video_id)
        return seen.played if seen else 0.0

    def watched(self, video_id: str) -> float:
        """How far into the video it has been played, the very end included.

        Unlike `resume`, this is the position as it stands: a video watched to
        the end reads as fully watched, even though there is nothing to resume.
        """
        seen = self.seen.get(video_id)
        return seen.at if seen else 0.0

    def resume(self, video_id: str) -> float:
        """Where a page opening this video should start. 0 means the beginning."""
        seen = self.seen.get(video_id)
        if seen is None or seen.at < MIN_RESUME:
            return 0.0
        if seen.duration and seen.at > seen.duration - END_MARGIN:
            return 0.0  # watched to the end
        return seen.at

    async def flush(self) -> None:
        if not self._dirty:
            return
        self._dirty = False
        if len(self.seen) > MAX_POSITIONS:
            keep = sorted(self.seen, key=self.last_played, reverse=True)[:MAX_POSITIONS]
            self.seen = {k: self.seen[k] for k in keep}
        payload = json.dumps({"seen": {k: asdict(v) for k, v in self.seen.items()}})
        try:
            await asyncio.to_thread(self._write, payload)
        except OSError as e:
            self._dirty = True  # try again on the next pass
            log.error("could not save %s: %s", self.path, e)

    def _write(self, payload: str) -> None:
        # Whole-file rewrite: it is a few hundred kilobytes at the very most.
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(payload)
        tmp.replace(self.path)
