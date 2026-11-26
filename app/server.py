import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit

import aiohttp
from aiohttp import web

from . import cast, config, sponsorblock
from .cast import CastError
from .jobs import PLAYLIST, Job, Jobs, playlist_from
from .store import Store
from .updater import Updater
from .youtube import VIDEO_ID, Library, VideoError, parse_video_id

log = logging.getLogger("server")

STATIC = Path(__file__).parent / "static"
MEDIA_FILE = re.compile(r"index\.m3u8|init\.mp4|\d{5,}\.m4s")
MEDIA_TYPES = {".m3u8": "application/vnd.apple.mpegurl", ".mp4": "video/mp4", ".m4s": "video/iso.segment"}
COOKIE = "yt_webplayer_auth"
COOKIE_VALUE = hmac.new(config.PASSWORD.encode(), b"yt-webplayer", hashlib.sha256).hexdigest()

JOBS = web.AppKey("jobs", Jobs)
LIBRARY = web.AppKey("library", Library)
STORE = web.AppKey("store", Store)
UPDATER = web.AppKey("updater", Updater)
HTTP = web.AppKey("http", aiohttp.ClientSession)

MAINTENANCE_SECONDS = 10


@dataclass
class Viewer:
    ws: web.WebSocketResponse
    video_id: str
    quality: int
    explicit: bool  # the page asked for this quality, rather than taking the default
    fresh: bool  # somebody just opened or reloaded the page, as opposed to a dropped socket
    job: Job | None = None
    duration: float = 0.0


# Nothing may load from anywhere but this server.
CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self' ws: wss:; media-src 'self' blob:; worker-src blob:; "
    "form-action 'self'; base-uri 'none'; frame-ancestors {}"
)

routes = web.RouteTableDef()


# ---------------------------------------------------------------- middleware


def _authorized(request: web.Request) -> bool:
    if hmac.compare_digest(request.cookies.get(COOKIE, "").encode(), COOKIE_VALUE.encode()):
        return True
    scheme, _, value = request.headers.get("Authorization", "").partition(" ")
    if scheme.lower() != "basic":
        return False
    try:
        _, _, password = base64.b64decode(value, validate=True).partition(b":")
    except (binascii.Error, ValueError):
        return False
    return hmac.compare_digest(password, config.PASSWORD.encode())


@web.middleware
async def auth(request: web.Request, handler):
    if not config.PASSWORD or request.path == "/healthz":
        return await handler(request)
    if not _authorized(request):
        return web.Response(status=401, text="Password required.", headers={"WWW-Authenticate": 'Basic realm="yt-webplayer"'})
    response = await handler(request)
    # Some browsers do not repeat Basic credentials on WebSockets; a cookie works everywhere.
    if not response.prepared and COOKIE not in request.cookies:
        response.set_cookie(COOKIE, COOKIE_VALUE, httponly=True, samesite="Lax", max_age=30 * 86400)
    return response


def _same_origin(request: web.Request) -> bool:
    """Guards anything that changes something, since there may be no password."""
    origin = urlsplit(request.headers.get("Origin", "")).netloc
    return origin in (request.host, request.headers.get("X-Forwarded-Host"))


async def security_headers(request: web.Request, response: web.StreamResponse) -> None:
    ancestors = "*" if request.path.startswith("/embed/") else "'self'"
    response.headers.setdefault("Content-Security-Policy", CSP.format(ancestors))
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")


# --------------------------------------------------------------------- pages


def page(status: int = 200) -> web.FileResponse:
    return web.FileResponse(STATIC / "index.html", status=status, headers={"Cache-Control": "no-cache"})


@routes.get("/")
async def home(request: web.Request):
    return page()


@routes.get("/watch")
async def watch(request: web.Request):
    video_id = request.query.get("v", "")
    if not VIDEO_ID.fullmatch(video_id):
        return page(404)
    # Ask YouTube right away instead of waiting for the page's script to load,
    # unless the video is already here and can answer for itself.
    # Not for other sites loading this address as an image, frame and so on.
    if (
        request.method == "GET"
        and request.headers.get("Sec-Fetch-Dest", "document") == "document"
        and not request.app[JOBS].on_disk(video_id)
    ):
        request.app[LIBRARY].prefetch(video_id)
    return page()


@routes.get(r"/embed/{id:[A-Za-z0-9_-]{11}}")
async def embed(request: web.Request):
    return page()


@routes.get(r"/{kind:shorts|live|v|e}/{id:[A-Za-z0-9_-]{11}}")
@routes.get(r"/{id:[A-Za-z0-9_-]{11}}")
async def to_watch(request: web.Request):
    query = {"v": request.match_info["id"], **{k: request.query[k] for k in ("t", "start") if k in request.query}}
    raise web.HTTPFound(f"/watch?{urlencode(query)}")


@routes.get("/go")
async def go(request: web.Request):
    text = request.query.get("url", "")
    if not (video_id := parse_video_id(text)):
        return page(404)
    query = {"v": video_id}
    if m := re.search(r"[?&#](?:t|start)=([\dhms]+)", text):
        query["t"] = m[1]
    raise web.HTTPFound(f"/watch?{urlencode(query)}")


@routes.get("/static/{name}")
async def static(request: web.Request):
    name = request.match_info["name"]
    if name not in ("app.js", "style.css", "hls.min.js"):
        raise web.HTTPNotFound
    return web.FileResponse(STATIC / name, headers={"Cache-Control": "no-cache"})


@routes.get("/favicon.ico")
async def favicon(request: web.Request):
    return web.FileResponse(STATIC / "icon.svg")


@routes.get("/healthz")
async def healthz(request: web.Request):
    version = request.app[UPDATER].version
    return web.json_response({"yt_dlp": version}, status=200 if version else 503)


# ----------------------------------------------------------------------- API


@routes.get("/api/config")
async def get_config(request: web.Request):
    return web.json_response(
        {
            "quality": config.DEFAULT_QUALITY,
            "sponsorblock": bool(config.SPONSORBLOCK_CATEGORIES),
            "vlc": bool(config.VLC_HOST),
            "ytDlp": request.app[UPDATER].version,
        }
    )


@routes.get("/api/videos")
async def videos(request: web.Request):
    """What the home page lists: on disk now, newest download first."""
    return web.json_response(request.app[JOBS].summary())


@routes.delete(r"/api/videos/{id:[A-Za-z0-9_-]{11}}/{quality:\d{1,5}}")
async def remove_video(request: web.Request):
    """The x on a row. Takes the files; leaves how far it was watched."""
    if not _same_origin(request):
        raise web.HTTPForbidden(text="Cross-origin request.")
    gone = request.app[JOBS].remove(request.match_info["id"], int(request.match_info["quality"]))
    return web.json_response({"removed": gone}, status=200 if gone else 404)


@routes.get(r"/api/thumbnail/{id:[A-Za-z0-9_-]{11}}")
async def thumbnail(request: web.Request):
    for name in ("maxresdefault", "hq720", "hqdefault"):
        url = f"https://i.ytimg.com/vi/{request.match_info['id']}/{name}.jpg"
        try:
            async with request.app[HTTP].get(url) as r:
                if r.status == 200:
                    return web.Response(body=await r.read(), content_type="image/jpeg", headers={"Cache-Control": "no-store"})
        except (aiohttp.ClientError, TimeoutError):
            break
    raise web.HTTPNotFound


@routes.get(r"/api/segments/{id:[A-Za-z0-9_-]{11}}")
async def segments(request: web.Request):
    """What the page skips for itself, drawing each one on the reel first."""
    return web.json_response(await sponsorblock.lookup(request.app[HTTP], request.match_info["id"]))


@routes.get(r"/media/{token:[\w-]+}/from-{offset:\d{1,6}}.m3u8")
async def media_from(request: web.Request):
    """The same video, as a playlist that starts part way in. See `cast`.

    Sits beside index.m3u8 rather than below it, so the segment names inside
    it, and #EXT-X-MAP, still resolve.
    """
    directory = config.MEDIA / request.match_info["token"]
    try:
        text = (directory / PLAYLIST).read_text()
    except OSError:
        raise web.HTTPNotFound from None
    # Asked for again every few seconds while the download runs, so the ranges
    # come off the disk beside the video and never from SponsorBlock itself.
    trimmed = playlist_from(text, float(request.match_info["offset"]), sponsorblock.remembered(directory))
    return web.Response(text=trimmed, content_type=MEDIA_TYPES[".m3u8"], headers={"Cache-Control": "no-store"})


@routes.get(r"/media/{token:[\w-]+}/{name}")
async def media(request: web.Request):
    name = request.match_info["name"]
    if not MEDIA_FILE.fullmatch(name):
        raise web.HTTPNotFound
    path = config.MEDIA / request.match_info["token"] / name
    if not path.is_file():
        raise web.HTTPNotFound
    # no-store: the browser must not keep its own copy on disk either.
    return web.FileResponse(path, headers={"Content-Type": MEDIA_TYPES[path.suffix], "Cache-Control": "no-store"})


@routes.post("/api/cast")
async def send_to_vlc(request: web.Request):
    """Hand what this page is playing, from where it is, to VLC elsewhere."""
    if not _same_origin(request):
        raise web.HTTPForbidden(text="Cross-origin request.")
    if not config.VLC_HOST:
        return web.json_response({"error": "No VLC address is configured."}, status=404)
    try:
        asked = await request.json()
        video_id, at = str(asked["v"]), float(asked["t"])
    except (ValueError, TypeError, KeyError):
        raise web.HTTPBadRequest(text="Expected a video and a position.") from None
    if not VIDEO_ID.fullmatch(video_id) or not 0 <= at < 86400:
        raise web.HTTPBadRequest(text="Not a video and a position.")

    job = request.app[JOBS].on_disk(video_id)
    if job is None or not job.seconds:
        return web.json_response({"error": "This video is not on the server yet."}, status=409)
    # Leave a little before the end, or VLC would open on the last segment.
    offset = int(min(at, max(0.0, job.seconds - 10)))
    host = request.headers.get("X-Forwarded-Host") or request.host
    if urlsplit(f"//{host}").hostname in ("127.0.0.1", "::1", "localhost"):
        # VLC would be fetching from itself. Nothing here can fix that.
        return web.json_response(
            {"error": "Open this page by the server's address on the network, not localhost, so that VLC can reach it."},
            status=409,
        )
    # VLC skips nothing itself, so the sponsors have to be out of what it is
    # handed. Looked up now and kept beside the video, because the playlist VLC
    # reloads is read straight off the disk. Finding none leaves what is there:
    # an unreachable SponsorBlock must not undo a lookup that did work.
    if found := await sponsorblock.lookup(request.app[HTTP], video_id):
        sponsorblock.remember(job.dir, found)
    skipped = len(sponsorblock.remembered(job.dir))
    # VLC fetches this itself, so it needs the password too, if there is one.
    credentials = f"vlc:{quote(config.PASSWORD, safe='')}@" if config.PASSWORD else ""
    url = f"{request.scheme}://{credentials}{host}/media/{job.token}/from-{offset}.m3u8"
    try:
        await cast.open_url(request.app[HTTP], url)
    except CastError as e:
        return web.json_response({"error": str(e)}, status=502)
    return web.json_response({"at": offset, "quality": job.quality, "skipped": skipped})


@routes.get("/api/ws")
async def socket(request: web.Request):
    """Carries progress out and the viewer's position back for as long as the page lives."""
    if not _same_origin(request):
        raise web.HTTPForbidden(text="Cross-origin request.")
    video_id = request.query.get("v", "")
    if not VIDEO_ID.fullmatch(video_id):
        raise web.HTTPBadRequest(text="Not a video ID.")
    quality = request.query.get("q", "")
    explicit = bool(re.fullmatch(r"\d{1,5}", quality))
    wanted = int(quality) if explicit else config.DEFAULT_QUALITY

    # The heartbeat notices viewers that vanish without saying goodbye.
    ws = web.WebSocketResponse(heartbeat=20, max_msg_size=1024)
    await ws.prepare(request)
    viewer = Viewer(ws, video_id, wanted, explicit, request.query.get("fresh") == "1")
    session = asyncio.create_task(_session(request.app, viewer))
    try:
        async for message in ws:
            if message.type is web.WSMsgType.TEXT:
                _report(request.app, viewer, message.data)
    finally:
        session.cancel()
        await asyncio.gather(session, return_exceptions=True)
    return ws


def _report(app: web.Application, viewer: Viewer, data: str) -> None:
    """The page says where it is, so that the next device can carry on there."""
    try:
        at = float(json.loads(data)["t"])
    except (ValueError, TypeError, KeyError):
        return
    if 0 <= at < 86400:  # also rejects NaN
        app[STORE].touch(viewer.video_id, at, viewer.duration)


async def _session(app: web.Application, viewer: Viewer) -> None:
    ws, jobs, library, store = viewer.ws, app[JOBS], app[LIBRARY], app[STORE]
    try:
        if viewer.fresh:
            # Somebody went here on purpose, so the video is wanted again.
            store.touch(viewer.video_id)
        elif jobs.forgotten(viewer.video_id):
            # A tab left open for days must not quietly download it all again.
            return await ws.send_json({"state": "expired"})
        # A video already here needs nothing from YouTube: it was downloaded
        # from a dump that is still on disk beside it, which is all the page
        # asks for. Only a video that is not here waits on an extraction.
        here = jobs.on_disk(viewer.video_id, viewer.quality if viewer.explicit else None)
        info = await here.described() if here else None
        if info is None:
            here = None
            if not app[UPDATER].ready.is_set():
                await ws.send_json({"state": "installing"})
            info = await library.info(viewer.video_id)
        viewer.duration = info.meta["duration"]
        quality = here.quality if here else jobs.quality_for(viewer.video_id, info, viewer.quality, viewer.explicit)
        job = viewer.job = jobs.acquire(viewer.video_id, quality, info)
        # Nothing to wait for when it is on disk; YouTube holds back only what it serves.
        choice = info.choices.get(job.quality)
        wait = choice.wait() if choice and not job.seconds else 0
        resume = store.resume(viewer.video_id)
        await ws.send_json({"meta": info.meta, "wait": round(wait), "resume": resume, **job.snapshot()})
        last = None
        while True:
            if jobs.expire(job):
                return await ws.send_json({"state": "expired"})
            snapshot = job.snapshot()
            if snapshot != last:
                await ws.send_json(snapshot)
                last = snapshot
            await asyncio.sleep(0.5 if snapshot["src"] else 0.1)
    except VideoError as e:
        await ws.send_json({"state": "error", "error": str(e)})
    except TimeoutError:
        await ws.send_json({"state": "error", "error": "yt-dlp is still being installed. Try again in a minute."})
    except ConnectionError:
        pass
    except Exception:
        log.exception("session for %s failed", viewer.video_id)
        await ws.send_json({"state": "error", "error": "Something went wrong on the server. Its log has the details."})
    finally:
        if viewer.job:
            jobs.release(viewer.job)


# Redirect plugins also send channels, playlists and searches here.
@routes.get("/{anything:.*}")
async def unsupported(request: web.Request):
    return page(404)


# ----------------------------------------------------------------- lifecycle


async def _maintain(app: web.Application) -> None:
    """Retention and saving positions: the upkeep no viewer sets off."""
    while True:
        await asyncio.sleep(MAINTENANCE_SECONDS)
        app[JOBS].sweep()
        await app[STORE].flush()


async def lifespan(app: web.Application):
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as http:
        app[HTTP] = http
        app[UPDATER] = Updater(http)
        app[LIBRARY] = Library(app[UPDATER])
        app[STORE] = Store()
        app[JOBS] = Jobs(app[LIBRARY], app[STORE])
        background = [asyncio.create_task(c) for c in (app[UPDATER].run(), _maintain(app))]
        yield
        for task in background:
            task.cancel()
        await asyncio.gather(*background, return_exceptions=True)
        await app[JOBS].close()
        await app[STORE].flush()


def make_app() -> web.Application:
    app = web.Application(middlewares=[auth])
    app.add_routes(routes)
    app.on_response_prepare.append(security_headers)
    app.cleanup_ctx.append(lifespan)
    return app


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")
    web.run_app(make_app(), port=8080, access_log=None, shutdown_timeout=3)
