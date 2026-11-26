"""Handing a video to VLC on another device.

VLC for Apple TV has no control interface of the kind desktop VLC offers. Its
Remote Playback screen runs a web server, and the page that server hands out
opens a WebSocket back to it and sends the URL to play. That one message is the
whole protocol. It carries no position, which is why what we send points at a
playlist that already begins where it should.
"""

import logging

import aiohttp

from . import config

log = logging.getLogger("cast")

class CastError(Exception):
    """A problem worth showing to the viewer."""


UNREACHABLE = (
    "Could not reach VLC at {}. Open VLC on the device and switch "
    "Remote Playback on in its settings."
)


async def open_url(http: aiohttp.ClientSession, url: str) -> None:
    """Tell VLC to play this now. Raises CastError if it cannot be told."""
    host = config.VLC_HOST
    try:
        async with http.ws_connect(f"ws://{host}/", timeout=aiohttp.ClientWSTimeout(ws_close=8)) as ws:
            await ws.send_json({"type": "openURL", "url": url})
            await ws.close()
    except (aiohttp.ClientError, TimeoutError, OSError) as e:
        log.warning("could not hand %s to VLC at %s: %s", url, host, e)
        raise CastError(UNREACHABLE.format(host)) from None
    log.info("handed %s to VLC at %s", url, host)
