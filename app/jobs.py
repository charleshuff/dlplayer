"""One job is one video at one quality on disk, shared by everyone watching it.

yt-dlp writes the video and audio streams into pipes and ffmpeg remuxes them
(without re-encoding) into HLS segments, so playback starts after the first
few seconds instead of after the whole download.

A video outlives the pages that watch it. What ends its life is time since it
was last played, which `Store` remembers across restarts, or pressure from a
newer video needing the room. Closing the page does not, and neither does
stopping the download: a tab closed halfway through finishes downloading, so
coming back finds the whole video there.
"""

import asyncio
import json
import logging
import os
import re
import secrets
import shutil
import signal
import time
from collections.abc import Sequence
from pathlib import Path

from . import config
from . import updater as up
from .store import Store
from .youtube import VIDEO_ID, YTDLP_ARGS, Info, Library, VideoError, error_message, parse_info

log = logging.getLogger("jobs")

PLAYLIST = "index.m3u8"
INFO = "info.json"  # the whole yt-dlp dump, which the download itself needs
DESCRIPTOR = "job.json"  # what a directory needs to be adopted after a restart
ENDLIST = "#EXT-X-ENDLIST"  # ffmpeg writes it once the whole video is on disk
ALIVE = ("downloading", "ready")


def playlist_seconds(text: str) -> float:
    return sum(float(m) for m in re.findall(r"^#EXTINF:([\d.]+)", text, re.M))


def _mostly_skipped(begin: float, seconds: float, skip: Sequence[tuple[float, float]]) -> bool:
    """Whether more of this segment falls in skipped time than out of it.

    Keeping a segment that is half sponsor leaves some of the sponsor; dropping
    one leaves out some of the video. Whichever there is more of wins, which
    costs at most half a segment either way.
    """
    covered = sum(max(0.0, min(end, begin + seconds) - max(start, begin)) for start, end in skip)
    return covered * 2 > seconds


def playlist_from(text: str, offset: float, skip: Sequence[tuple[float, float]] = ()) -> str:
    """The same playlist as it would read had the video begun `offset` in and
    the time in `skip` never been in it.

    Handing VLC a URL is the only thing its remote playback protocol allows, so
    starting it part way through means giving it a playlist that starts there,
    and skipping sponsors means giving it one they are not in -- VLC itself
    skips nothing. Whole segments only, which is as fine-grained as HLS gets.
    """
    pairs: list[tuple[str, str]] = []
    header: list[str] = []
    tail: list[str] = []
    i = 0
    lines = text.splitlines()
    while i < len(lines):
        if lines[i].startswith("#EXTINF") and i + 1 < len(lines):
            pairs.append((lines[i], lines[i + 1]))
            i += 2
        else:
            (tail if pairs else header).append(lines[i])  # #EXT-X-MAP above, #EXT-X-ENDLIST below
            i += 1

    # Where each segment falls in the whole video, and whether it survives.
    spans: list[tuple[float, float]] = []
    at = 0.0
    for extinf, _ in pairs:
        seconds = float(re.match(r"#EXTINF:([\d.]+)", extinf)[1])
        spans.append((at, seconds))
        at += seconds
    keep = [not _mostly_skipped(begin, seconds, skip) for begin, seconds in spans]
    if not any(keep):
        keep = [True] * len(pairs)  # a video that is all sponsor is a bad answer

    # The first segment kept that the position has not gone past already, or
    # failing that the last one: past the end, the end will do.
    start = next(
        (i for i, (begin, seconds) in enumerate(spans) if keep[i] and begin + seconds > offset),
        max((i for i, kept in enumerate(keep) if kept), default=0),
    )

    body: list[str] = []
    cut = False
    for i in range(start, len(pairs)):
        if not keep[i]:
            cut = True
            continue
        if cut:
            body.append("#EXT-X-DISCONTINUITY")  # what follows it is not when it says
            cut = False
        body.extend(pairs[i])

    return "\n".join(
        [f"#EXT-X-MEDIA-SEQUENCE:{start}" if ln.startswith("#EXT-X-MEDIA-SEQUENCE") else ln for ln in header]
        + body
        + tail
    ) + "\n"


def too_old(last_played: float) -> bool:
    """Past the retention window. A missing timestamp counts as long ago."""
    return 0 < config.UNPLAYED_SECONDS <= time.time() - last_played


def _tail(path: Path) -> str:
    try:
        return path.read_text(errors="replace")[-4000:]
    except OSError:
        return ""


def _downloaded_with(path: Path) -> tuple[str, str, float]:
    """How a video labels itself, read back from the dump it was downloaded with.

    Descriptors have gained fields over time, so this fills in whichever of
    them an older one is missing.
    """
    try:
        described = json.loads((path / INFO).read_bytes())
        return (
            str(described.get("title") or ""),
            str(described.get("channel") or described.get("uploader") or ""),
            float(described.get("duration") or 0),
        )
    except (OSError, ValueError, TypeError):
        return "", "", 0.0


def _finished(playlist: Path) -> bool:
    try:
        return ENDLIST in playlist.read_text(errors="replace")
    except OSError:
        return False


class Job:
    def __init__(
        self,
        video_id: str,
        quality: int,
        info: Info | None,
        *,
        token: str = "",
        started: float = 0.0,
        title: str = "",
        channel: str = "",
        duration: float = 0.0,
    ):
        self.video_id = video_id
        self.quality = quality
        self.info = info  # None for a job adopted after a restart, which never downloads
        self.token = token or secrets.token_urlsafe(9)
        self.dir = config.MEDIA / self.token
        self.state = "downloading"
        self.error: str | None = None
        self.seconds = 0.0
        self.bytes = 0
        self.viewers = 0
        self.deleted = False
        # Wall clock, because it keeps counting while the server is suspended.
        self.started = started or time.time()
        meta = info.meta if info else {}
        self.title = title or meta.get("title") or video_id
        self.channel = channel or meta.get("channel") or ""
        self.duration = duration or meta.get("duration") or 0.0
        self.task: asyncio.Task | None = None
        self._described: Info | None = None  # the dump read back off disk, parsed once

    @classmethod
    def adopt(cls, path: Path) -> "Job | None":
        """Take over a video left on disk by an earlier run, if it is complete.

        A download interrupted by a restart is not resumable -- ffmpeg cannot
        append to the playlist it wrote -- so only finished ones come back.
        """
        if not _finished(path / PLAYLIST):
            return None
        try:
            described = json.loads((path / DESCRIPTOR).read_bytes())
            video_id, quality = str(described["id"]), int(described["quality"])
            # Descriptors written before the home page existed carry neither the
            # download time nor anything to label the video with.
            started = float(described.get("started") or path.stat().st_mtime)
            title = str(described.get("title") or "")
            channel = str(described.get("channel") or "")
            duration = float(described.get("duration") or 0)
        except (OSError, ValueError, TypeError, KeyError):
            return None
        if not VIDEO_ID.fullmatch(video_id):
            return None
        if not title or not channel:
            was_title, was_channel, was_duration = _downloaded_with(path)
            title, channel = title or was_title, channel or was_channel
            duration = duration or was_duration
        job = cls(
            video_id, quality, None, token=path.name,
            started=started, title=title, channel=channel, duration=duration,
        )  # fmt: skip
        job.state = "ready"
        job._measure()
        return job if job.seconds else None

    def snapshot(self) -> dict:
        return {
            "state": self.state,
            "error": self.error,
            "quality": self.quality,
            "src": f"/media/{self.token}/{PLAYLIST}" if self.seconds else None,
            "seconds": round(self.seconds, 2),
            "bytes": self.bytes,
        }

    async def described(self) -> Info | None:
        """What a page needs to show this video, without a word to YouTube.

        The dump the download was made from sits beside the segments, so a
        video already here describes itself. The media URLs in it expired
        hours ago, which costs nothing: there is nothing left to fetch. None
        means the dump is unreadable and YouTube has to be asked after all.
        """
        if self.info:
            return self.info
        if self._described is None:
            try:
                self._described = await asyncio.to_thread(lambda: parse_info((self.dir / INFO).read_bytes()))
            except (OSError, ValueError, TypeError, KeyError, VideoError) as e:
                log.warning("%s is here but its own dump will not be read: %s", self.video_id, e)
        return self._described

    async def run(self, library: Library) -> None:
        try:
            try:
                await self._download()
            except VideoError as e:
                if self.seconds:
                    raise
                # Nothing played yet: expired URLs or an outdated yt-dlp are the
                # usual causes, so start over once with fresh ones.
                log.warning("download of %s failed, retrying: %s", self.video_id, e)
                await library.updater.check(min_interval=300)
                library.forget(self.video_id)
                self.info = await library.info(self.video_id)
                if self.quality not in self.info.choices:
                    raise
                await self._download()
            self.state = "ready"
            log.info("finished %s at %sp, %.0f MB", self.video_id, self.quality, self.bytes / 1e6)
        except VideoError as e:
            self.state, self.error = "error", str(e)
            log.warning("download of %s failed: %s", self.video_id, e)
        except Exception:
            self.state, self.error = "error", "Something went wrong on the server. Its log has the details."
            log.exception("download of %s crashed", self.video_id)

    async def _download(self) -> None:
        choice = self.info.choices[self.quality]
        shutil.rmtree(self.dir, ignore_errors=True)
        self.dir.mkdir(parents=True)
        (self.dir / INFO).write_bytes(self.info.raw)
        (self.dir / DESCRIPTOR).write_text(
            json.dumps(
                {
                    "id": self.video_id,
                    "quality": self.quality,
                    "started": self.started,
                    "title": self.title,
                    "channel": self.channel,
                    "duration": self.duration,
                }
            )
        )
        procs: list[asyncio.subprocess.Process] = []
        logs: list[Path] = []
        read_ends: list[int] = []

        async def spawn(name: str, args: list[str], **kw) -> None:
            logs.append(self.dir / f"{name}.log")
            with logs[-1].open("wb") as stderr:
                procs.append(
                    await asyncio.create_subprocess_exec(
                        *args, stdin=asyncio.subprocess.DEVNULL, stderr=stderr, start_new_session=True, **kw
                    )
                )

        try:
            for i, format_id in enumerate(choice.formats):
                read_end, write_end = os.pipe()
                read_ends.append(read_end)
                try:
                    await spawn(
                        f"yt-dlp{i}",
                        [
                            *YTDLP_ARGS, "--quiet", "--fixup", "never",
                            "--load-info-json", str(self.dir / INFO),
                            "-f", format_id, "-o", "-",
                        ],
                        stdout=write_end, env=up.environment(), cwd=self.dir,
                    )  # fmt: skip
                finally:
                    os.close(write_end)

            args = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "warning"]
            for fd in read_ends:
                args += ["-i", f"pipe:{fd}"]
            args += ["-map", "0:v:0", "-map", "1:a:0"] if len(read_ends) == 2 else ["-map", "0:v:0", "-map", "0:a:0?"]
            args += [
                "-c", "copy", "-f", "hls", "-hls_time", "4", "-hls_playlist_type", "event",
                "-hls_segment_type", "fmp4", "-hls_flags", "independent_segments+temp_file",
                "-hls_segment_filename", str(self.dir / "%05d.m4s"), str(self.dir / PLAYLIST),
            ]  # fmt: skip
            await spawn("ffmpeg", args, stdout=asyncio.subprocess.DEVNULL, pass_fds=read_ends)
            while read_ends:
                os.close(read_ends.pop())

            pending = {asyncio.create_task(p.wait()) for p in procs}
            try:
                while pending:
                    done, pending = await asyncio.wait(
                        pending, timeout=1 if self.seconds else 0.1, return_when=asyncio.FIRST_COMPLETED
                    )
                    if any(t.result() for t in done):
                        await asyncio.sleep(0.5)  # let the culprit finish writing its log
                        errors = "\n".join(map(_tail, logs))
                        log.warning("%s: %s", self.video_id, errors.strip()[-1000:])
                        raise VideoError(f"The download stopped. {error_message(errors)}")
                    self._measure()
                    if shutil.disk_usage(self.dir).free < config.MIN_FREE_BYTES:
                        raise VideoError("The server's disk is almost full, so the download was stopped.")
            finally:
                for t in pending:
                    t.cancel()
            self._measure()
            if not self.seconds:
                raise VideoError("The download produced no video.")
        finally:
            for fd in read_ends:
                os.close(fd)
            for p in procs:
                if p.returncode is None:
                    try:
                        os.killpg(p.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def _measure(self) -> None:
        try:
            self.seconds = playlist_seconds((self.dir / PLAYLIST).read_text())
            self.bytes = sum(e.stat().st_size for e in os.scandir(self.dir) if e.name.endswith(".m4s"))
        except FileNotFoundError:
            pass  # not written yet, or a temp file was renamed while scanning


class Jobs:
    def __init__(self, library: Library, store: Store):
        self.library = library
        self.store = store
        self.jobs: dict[tuple[str, int], Job] = {}
        self._deleting: set[asyncio.Task] = set()
        config.MEDIA.mkdir(parents=True, exist_ok=True)
        self._adopt_all()

    # ------------------------------------------------------------- lifetime

    def _adopt_all(self) -> None:
        """Pick up where the last run left off, discarding the rest."""
        for path in sorted(config.MEDIA.iterdir()):
            job = Job.adopt(path) if path.is_dir() else None
            if job is None or (job.video_id, job.quality) in self.jobs or self.forgotten(job.video_id):
                if path.is_dir():
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    path.unlink(missing_ok=True)
                continue
            self.jobs[job.video_id, job.quality] = job
            log.info("kept %s at %sp, %.0f MB", job.video_id, job.quality, job.bytes / 1e6)

    def _played(self, job: Job) -> float:
        """When this video was last played, or failing that, when it arrived."""
        return max(self.store.last_played(job.video_id), job.started)

    def forgotten(self, video_id: str) -> bool:
        """Watched here before, but too long ago for anything to be kept."""
        played = self.store.last_played(video_id)
        return bool(played) and too_old(played)

    def expire(self, job: Job) -> bool:
        """Delete a video nobody has played for too long, open page or not."""
        if not job.deleted and too_old(self._played(job)):
            job.state = "expired"
            self._delete(job)
        return job.state == "expired"

    def sweep(self) -> None:
        for job in list(self.jobs.values()):
            self.expire(job)

    def listing(self) -> list[dict]:
        """Everything on disk, the most recently started download first."""
        here = sorted((j for j in self.jobs.values() if j.state in ALIVE), key=lambda j: j.started, reverse=True)
        return [
            {
                "id": j.video_id,
                "title": j.title,
                "channel": j.channel,
                "quality": j.quality,
                "state": j.state,
                "seconds": round(j.seconds, 1),
                "duration": j.duration,
                "watched": round(self.store.watched(j.video_id), 1),
                "bytes": j.bytes,
            }
            for j in here
        ]

    def summary(self) -> dict:
        """The rows the home page draws, and what they come to."""
        rows = self.listing()
        return {
            "videos": rows,
            "bytes": sum(r["bytes"] for r in rows),
            "free": shutil.disk_usage(config.MEDIA).free,
        }

    def on_disk(self, video_id: str, quality: int | None = None) -> Job | None:
        """The copy a page can be given straight away, without asking YouTube.

        The best one here, or the one at `quality` when the page asked for a
        quality itself -- the rule `quality_for` follows, applied before the
        extraction rather than after it. Also what gets handed to VLC.
        """
        here = [
            j
            for j in self.jobs.values()
            if j.video_id == video_id and j.state in ALIVE and quality in (None, j.quality)
        ]
        return max(here, key=lambda j: j.quality) if here else None

    def remove(self, video_id: str, quality: int) -> bool:
        """Throw one away now, on request. Where it was watched is kept."""
        job = self.jobs.get((video_id, quality))
        if job is None:
            return False
        # Marked as gone rather than merely deleted, so that a page still
        # showing it says so instead of quietly downloading it again.
        job.state = "expired"
        self._delete(job)
        log.info("removed %s at %sp on request", video_id, quality)
        return True

    # -------------------------------------------------------------- viewers

    def quality_for(self, video_id: str, info: Info, wanted: int, explicit: bool) -> int:
        """Reuse what is already here unless this page asked for a quality itself.

        Without this, two devices with different defaults would each download
        their own copy of the same video and hold two slots for three days.
        """
        if not explicit:
            here = [q for (vid, q), job in self.jobs.items() if vid == video_id and job.state in ALIVE and q in info.choices]
            if here:
                return max(here)
        return info.pick(wanted)

    def acquire(self, video_id: str, quality: int, info: Info) -> Job:
        """Synchronous on purpose, so a cancelled viewer can never leak a job."""
        key = (video_id, quality)
        job = self.jobs.get(key)
        if job and job.state == "error":
            self._delete(job)
            job = None
        if job is None:
            self._make_room(info.choices[quality].size)
            job = self.jobs[key] = Job(video_id, quality, info)
            self.store.touch(video_id)  # opening a video counts as using it
            job.task = asyncio.create_task(job.run(self.library))
            log.info("downloading %s at %sp", video_id, quality)
        job.viewers += 1
        return job

    def release(self, job: Job) -> None:
        """Nothing happens: the video's lifetime does not depend on its pages."""
        job.viewers -= 1

    def _make_room(self, size: int) -> None:
        """Evict least recently played videos until one more of `size` fits."""
        reclaimed = 0
        while True:
            crowded = len(self.jobs) >= config.MAX_VIDEOS
            tight = shutil.disk_usage(config.MEDIA).free + reclaimed - size < config.MIN_FREE_BYTES
            if not crowded and not tight:
                return
            # A video someone is watching is not something to throw away.
            spare = [j for j in self.jobs.values() if not j.viewers]
            if not spare:
                raise VideoError(
                    f"{config.MAX_VIDEOS} videos are open in other pages. Close one and try again."
                    if crowded
                    else "The server does not have enough free disk space for this video. Try a lower quality."
                )
            victim = min(spare, key=self._played)
            reclaimed += victim.bytes  # the files go in the background, so count it here
            self._delete(victim)

    # ------------------------------------------------------------- deleting

    def _delete(self, job: Job) -> None:
        if job.deleted:
            return
        job.deleted = True
        if self.jobs.get((job.video_id, job.quality)) is job:
            del self.jobs[job.video_id, job.quality]
        task = asyncio.create_task(self._destroy(job))
        self._deleting.add(task)
        task.add_done_callback(self._deleting.discard)

    async def _destroy(self, job: Job) -> None:
        if job.task:
            job.task.cancel()
            await asyncio.gather(job.task, return_exceptions=True)
        await asyncio.to_thread(shutil.rmtree, job.dir, ignore_errors=True)
        log.info("deleted %s at %sp", job.video_id, job.quality)

    async def close(self) -> None:
        """Stop downloading, but leave on disk whatever is finished.

        Killed mid-write, ffmpeg never gets to close its playlist, so a partial
        download is recognisable as one and cleaned up on the next start.
        """
        running = [j.task for j in self.jobs.values() if j.task]
        for task in running:
            task.cancel()
        await asyncio.gather(*running, *self._deleting, return_exceptions=True)
