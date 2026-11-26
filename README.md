# yt-webplayer

A small, self-hosted replacement for watching YouTube through Invidious. Open a
video and the server downloads it with [yt-dlp](https://github.com/yt-dlp/yt-dlp)
and plays it while it is still downloading. It keeps the video for a few days and
remembers where you stopped, so the next device you open it on carries on from there.

*Vibe coded: this was written by Claude Code from prompts rather than typed out by
hand. It is reviewed and it runs, but read it with that in mind.*

- Starts within a few seconds, at any quality YouTube offers (up to 4K)
- Kept for three days after the last time anyone played it, then deleted
- Picks up where you left off, whichever device you open it on next
- The home page lists what is on the server, newest first, with how far you watched
  each one, what it all takes up, and an x to delete one
- The browser only ever talks to your server, never to YouTube or Google
- yt-dlp updates itself, with no restarts or image rebuilds
- SponsorBlock skipping and a quality selector, skipped on VLC too
- Works as a redirect target for YouTube links

It plays single videos. Search, channels, playlists and livestreams are not included.

## Run it

You need Docker with the Compose plugin.

```sh
git clone https://github.com/charleshuff/dlplayer.git
cd dlplayer
docker compose up -d --build
```

Open `http://<server>:3000`. The first start takes a few extra seconds to install yt-dlp.

To change settings, `cp .env.example .env`, edit it, and run `docker compose up -d` again.

## Redirect YouTube links to it

The URLs match Invidious and YouTube (`/watch?v=`, `/embed/`, `/shorts/`, `/live/`, `/<id>`),
so anything that can redirect to an Invidious instance works.

**[LibRedirect](https://libredirect.github.io/)** (recommended): in its settings choose
YouTube, set the frontend to Invidious, add `http://<server>:3000` as a custom instance,
and deselect the public ones.

**Privacy Redirect** also accepts a custom Invidious instance, but the project is archived
and no longer maintained.

**Any regex redirector**, such as Redirector:

| | |
|---|---|
| Pattern | `^https?://(?:www\.\|m\.)?youtube\.com/(watch\?.*\|shorts/.*\|live/.*)` |
| Redirect to | `http://<server>:3000/$1` |

Embedded videos on other sites start downloading when they scroll into view. If your server is
plain HTTP, browsers block it inside HTTPS pages, so redirecting embeds needs HTTPS.

## Send it to VLC

Set `VLC_HOST` in `.env` to the address of a device running VLC and the player gets a
**Send to VLC** button, which hands the video over from wherever you had got to.

On an Apple TV, open VLC and switch Remote Playback on in its settings first. That is
what listens; nothing can be sent while it is off. Its protocol accepts a URL and nothing
else, so the position travels in the URL: what VLC is given is a playlist that starts at
the right segment, which also means its own timeline starts from there rather than from
the beginning of the video.

VLC skips nothing by itself, so the playlist it is given is also one the SponsorBlock
segments are not in -- the sponsors are simply missing from what plays, and VLC's
timeline is shorter by as much. Whole segments go or stay, about four seconds each, so a
second or two of a sponsor can survive at either end of it; nothing is re-encoded and
nothing is deleted, so the browser keeps skipping to the exact second and still has the
whole video to seek through. Set `SPONSORBLOCK_CATEGORIES=` to leave VLC the sponsors
as well.

VLC fetches the video from this server, so open the page by the server's address on the
network rather than localhost, or the address handed over will not mean anything on the
other device. If `PASSWORD` is set, it goes along in that URL.

## How it works

yt-dlp fetches the video and audio streams in parallel and pipes them into ffmpeg, which
remuxes them (no re-encoding, so little CPU) into HLS segments on disk. Closing the page
does not stop that: a video opened and then abandoned finishes downloading, so coming back
later finds all of it there.

What ends a video's life is time. `UNPLAYED_DAYS` (default 3) after the last time anyone
played it, it is deleted, and a page still showing it offers to download it again. Until
then it survives closed tabs and restarts of the server. Videos also make way for each
other: when `MAX_VIDEOS` or `MIN_FREE_GB` is reached, the one played longest ago goes
first. A download cut short by a restart cannot be resumed, because ffmpeg cannot append
to the playlist it already wrote, so those are cleaned up on the next start.

Positions live in `/data/state.json` and outlive the files they point into, so a video
deleted for age is downloaded again on the next visit and still resumes where it was.
There are no accounts here, so a position belongs to the video rather than to a person,
which is exactly what lets the phone carry on from where the laptop stopped.

Video at 1440p and above is VP9 or AV1, which some older devices cannot play; 1080p and
below is H.264 wherever YouTube offers it.

### Keeping yt-dlp current

YouTube changes often, and outdated yt-dlp is the usual reason things break. The server
checks PyPI at startup, every `UPDATE_HOURS` (default 3), and whenever a video fails, in
which case it retries with the new version. Each version is installed next to the old one,
together with the JavaScript runtime it needs, and only takes over if it runs. Nightly
builds are the default because YouTube fixes arrive there first; set
`YTDLP_CHANNEL=stable` for releases only. The current version is shown on the home page.

The rest of the image (Python, ffmpeg) is updated by rebuilding now and then:

```sh
git pull && docker compose build --pull && docker compose up -d
```

## Exposing it

By default there is no login, which is fine on a home network or VPN. Set `PASSWORD` in
`.env` to require one. Use HTTPS if it crosses an untrusted network. Behind a reverse
proxy, bind to `127.0.0.1`, allow WebSocket upgrades, and pass the original `Host` header
(or set `X-Forwarded-Host`).

Everyone who can reach the server shares one set of playback positions, and whatever was
watched in the last few days is still sitting on the volume, so treat access to it as
access to a viewing history.

## Troubleshooting

`docker compose logs -f` shows what yt-dlp says. "Sign in to confirm you're not a bot"
means YouTube distrusts the server's IP address, which is common for data centres and
VPN exits and rare on home connections. Age-restricted and members-only videos need an
account and do not play.

Tests: `docker run --rm -v ./tests:/srv/tests:ro yt-webplayer python -m unittest tests.test_youtube`
