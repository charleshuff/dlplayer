"""Keeps yt-dlp (plus its JS challenge solver and Deno runtime) up to date.

Every version gets its own virtualenv under /data/yt-dlp, and a `current`
symlink is swapped to it only after a smoke test. Downloads already running
keep using the version they started with, a broken release never replaces a
working one, and no restart is needed: yt-dlp runs as a subprocess, so the
next video simply uses the new version.
"""

import asyncio
import logging
import os
import shutil
import sys
import time

import aiohttp
from packaging.version import InvalidVersion, Version

from . import config

log = logging.getLogger("updater")

INDEX = "https://pypi.org/simple/yt-dlp/"
CURRENT = config.YTDLP_ROOT / "current"
# Changing the suffix makes existing installs rebuild themselves.
PYTHON_TAG = f"py{sys.version_info.major}.{sys.version_info.minor}-r2"


def environment() -> dict[str, str]:
    """Environment in which `yt-dlp` and `deno` resolve to the current version."""
    bin_dir = CURRENT.resolve() / "bin"
    env = {k: v for k, v in os.environ.items() if k != "PASSWORD"}
    return env | {"PATH": f"{bin_dir}:{os.environ['PATH']}", "DENO_NO_UPDATE_CHECK": "1"}


def newest(index: dict, channel: str) -> Version | None:
    """Pick the newest non-yanked release from a PyPI simple-API document."""
    best = None
    for file in index["files"]:
        if file.get("yanked") or not file["filename"].endswith(".whl"):
            continue
        try:
            version = Version(file["filename"].split("-")[1])
        except (InvalidVersion, IndexError):
            continue
        if version.is_prerelease and channel != "nightly":
            continue
        if best is None or version > best:
            best = version
    return best


async def _run(*args: str, env: dict | None = None, timeout: float = 600) -> str:
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=env,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except BaseException:
        if proc.returncode is None:
            proc.kill()
        raise
    text = out.decode(errors="replace").strip()
    if proc.returncode:
        raise RuntimeError(f"{args[0]} failed: {text[-800:]}")
    return text


async def _smoke_test(venv) -> str:
    version = await _run(str(venv / "bin/yt-dlp"), "--version", timeout=60)
    await _run(str(venv / "bin/deno"), "--version", timeout=60)
    return version


class Updater:
    def __init__(self, http: aiohttp.ClientSession):
        self.http = http
        self.version: str | None = None
        self.ready = asyncio.Event()
        self._lock = asyncio.Lock()
        self._last_check = 0.0

    async def run(self) -> None:
        config.YTDLP_ROOT.mkdir(parents=True, exist_ok=True)
        if CURRENT.exists():
            try:
                self.version = await _smoke_test(CURRENT.resolve())
                self.ready.set()
                log.info("using yt-dlp %s", self.version)
            except (RuntimeError, OSError, TimeoutError) as e:
                log.warning("installed yt-dlp does not run, reinstalling: %s", e)
                CURRENT.unlink()
        while True:
            await self.check()
            # Retry quickly while there is nothing usable at all.
            await asyncio.sleep(config.UPDATE_SECONDS if self.ready.is_set() else 30)

    async def check(self, min_interval: float = 0) -> bool:
        """Update if a newer version exists. Returns True if one was installed."""
        before = self.version
        async with self._lock:
            # Someone else may have updated while we waited for the lock.
            if self.version != before:
                return True
            if time.monotonic() - self._last_check < min_interval:
                return False
            try:
                return await self._check()
            except (RuntimeError, OSError, TimeoutError, aiohttp.ClientError) as e:
                log.error("update failed, keeping yt-dlp %s: %s", self.version, e)
                return False
            finally:
                self._last_check = time.monotonic()

    async def _check(self) -> bool:
        headers = {"Accept": "application/vnd.pypi.simple.v1+json"}
        async with self.http.get(INDEX, headers=headers) as r:
            r.raise_for_status()
            target = newest(await r.json(content_type=None), config.YTDLP_CHANNEL)
        if target is None:
            raise RuntimeError("no yt-dlp release found on PyPI")

        venv = config.YTDLP_ROOT / f"{target}-{PYTHON_TAG}"
        if self.ready.is_set() and CURRENT.resolve() == venv:
            return False

        log.info("installing yt-dlp %s", target)
        shutil.rmtree(venv, ignore_errors=True)
        try:
            await _run(sys.executable, "-m", "venv", str(venv))
            # Compiled to bytecode by pip, which more than halves yt-dlp's startup
            # time. Upper bounds are left to yt-dlp, which pins the solver version and
            # the minimum Deno version it needs.
            await _run(
                str(venv / "bin/pip"), "install", "--quiet", "--no-cache-dir",
                "--disable-pip-version-check", f"yt-dlp[default,deno]=={target}",
            )  # fmt: skip
            version = await _smoke_test(venv)
        except BaseException:
            shutil.rmtree(venv, ignore_errors=True)
            raise

        previous = CURRENT.resolve() if CURRENT.exists() else None
        tmp = config.YTDLP_ROOT / "current.new"
        tmp.unlink(missing_ok=True)
        tmp.symlink_to(venv.name)
        tmp.replace(CURRENT)
        self.version = version
        self.ready.set()
        log.info("now using yt-dlp %s", version)

        # The previous version stays for downloads that still run on it.
        for path in config.YTDLP_ROOT.iterdir():
            if path.is_dir() and not path.is_symlink() and path not in (venv, previous):
                shutil.rmtree(path, ignore_errors=True)
        return True
