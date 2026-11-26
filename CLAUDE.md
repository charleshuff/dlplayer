# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

Everything runs in Docker; the host has no Python dependencies installed.

```sh
docker compose up -d --build          # build and run on http://localhost:3000
docker compose logs -f                # yt-dlp output lands here
```

`app/` is `COPY`d into the image, not mounted, so editing it means `up -d --build`
again; `docker compose restart` would start the old code. Only `/data` survives a
rebuild, so downloads and positions are kept.

```sh
docker run --rm -v ./tests:/srv/tests:ro yt-webplayer python -m unittest discover -s tests -t .
docker run --rm -v ./tests:/srv/tests:ro yt-webplayer python -m unittest tests.test_youtube.ChooseFormats.test_combined_only
```

`tests` is in `.dockerignore`, which is why it is mounted rather than built in. The
image must be current for the tests to mean anything, so rebuild before running them.
`config` is module-level constants read at import, so tests patch the attribute rather
than the environment (`mock.patch.object(config, "MEDIA", tmp)`).

There is no linter or formatter config. Some call sites carry `# fmt: skip` to keep
hand-wrapped argument lists intact.

Settings live in `.env` (copy from `.env.example`); `compose.yaml` passes each one
through as an environment variable, and `app/config.py` is the only place that reads
settings out of the environment. Adding a setting means touching all three. (`DATA_DIR`
is the exception: `config.py` honours it, nothing passes it, and it exists so the app
can be pointed somewhere other than `/data`.)

## Architecture

A single aiohttp process. `/data` holds everything that outlives it: `media/` the HLS
segments, `yt-dlp/` the installed virtualenvs, `state.json` the playback positions.

**`app/server.py`** — routes, auth middleware, CSP, and the WebSocket session loop.
Long-lived objects (`Updater`, `Library`, `Jobs`, the shared `ClientSession`) are built
in `lifespan` and reached through `web.AppKey` constants.

**`app/youtube.py`** — URL parsing, `yt-dlp -J` metadata extraction, and format
selection. `Library` caches the extraction as an `asyncio.Task` per video ID so a
quality change does not re-ask YouTube; tasks are shielded so one viewer disconnecting
cannot cancel the fetch for another.

**`app/store.py`** — `state.json`: where each video was last stopped and when it was
last played. Rewritten whole on a dirty flag, atomically via tmp-and-replace.

**`app/jobs.py`** — one `Job` is one video at one quality on disk, keyed by
`(video_id, quality)` and shared by every viewer of that combination. The download is
`yt-dlp` writing each chosen format into an `os.pipe()`, with `ffmpeg` reading those
pipe FDs and remuxing (`-c copy`) into HLS fMP4 segments. Progress is measured by
summing `#EXTINF` durations out of the playlist on disk, not by parsing yt-dlp output.

**`app/updater.py`** — installs each yt-dlp version into its own virtualenv and swaps a
`current` symlink only after a smoke test, so a broken release never replaces a working
one and no restart is needed.

**`app/cast.py`** — the one message VLC's Remote Playback protocol takes. Thirty lines,
separate because the reasoning behind the URL it is given lives in `jobs.playlist_from`.

**`app/sponsorblock.py`** — the only place that talks to SponsorBlock, for the page that
skips the segments itself and for the playlist VLC is handed, which cannot. It also keeps
the ranges beside the segments as `sponsor.json`, and merges overlapping categories into
the (start, end) pairs `playlist_from` cuts with.

**`app/static/index.html` + `app.js`** — one page and one script, no framework, no build
step, serving both halves of the site: `library`, `home`, `unsupported` and `player` are
sections toggled by `hidden` according to the path. `app.js` drives hls.js, the custom
reel (download progress + SponsorBlock marks + playhead), and reconnection.

### Things that follow from the design

- **One timestamp drives both features.** `Store` records when a video was last played;
  that is the resume point's clock *and* the retention clock. Pages do not own videos —
  `release` only decrements a count used to decide what may be evicted, so closing a tab
  neither deletes the video nor stops its download. `acquire` is still synchronous on
  purpose: an `await` inside it would let a cancelled viewer leak a job.
- **Positions outlive the files they point into.** A video deleted for age is downloaded
  again on the next visit and still resumes, so `Store` entries are pruned by count
  (`MAX_POSITIONS`), never by the retention window.
- **Three things end a video's life by themselves**, all in `jobs.py`: `sweep()` from the
  maintenance loop (nothing else polls expiry once viewers can be absent), `_make_room`
  evicting the least recently played when `MAX_VIDEOS`/`MIN_FREE_GB` bites, and
  `_adopt_all` at startup. `remove` is the fourth and only deliberate one. Eviction counts
  bytes it is *about* to free, because `rmtree` runs in a background task and
  `disk_usage` would not see it yet.
- **A restart keeps finished videos only.** `Job.adopt` requires `#EXT-X-ENDLIST` in the
  playlist — ffmpeg writes it on a clean exit and cannot append to a playlist it already
  closed, so an interrupted download is detectable and unresumable. `job.json` carries
  the id and quality, and the id is re-validated against `VIDEO_ID` coming off disk
  because it reaches a subprocess argv.
- **`job.json` also carries what the home page shows** — `started`, `title`, `channel`,
  `duration` — so `/api/videos` need not re-parse the multi-megabyte `info.json`, and the
  list keeps its order across restarts (`started` would otherwise reset to adoption
  time). The descriptor has gained fields over time, so `Job.adopt` fills in each one a
  given generation lacks from `info.json`, and the download time from the directory's
  mtime; test both when adding a field.
- **A video already here never waits on YouTube.** `Jobs.on_disk` finds the copy a
  page can be given — the best one, or the exact quality a page asked for itself — and
  `Job.described()` rebuilds the page's metadata from the `info.json` beside the
  segments, so re-opening a downloaded video starts playing in milliseconds rather than
  behind a 5-second `yt-dlp -J`. The media URLs in that dump expired hours ago, which
  costs nothing when nothing is being fetched; an unreadable dump returns None and the
  extraction happens after all. `/watch` skips its prefetch for the same reason, and a
  copy on disk plays even while yt-dlp is being reinstalled.
- **A row's bar is playback, not download**, so it comes from `Store.watched` — the raw
  position, as distinct from `Store.resume`, which deliberately returns 0 at either end
  of the video. `seconds` in the payload is still download progress, used only to stand
  in for an unknown duration and to label a row as still downloading.
- **A quality already on disk wins over the viewer's default.** `quality_for` only
  honours what a page asked for when it asked explicitly (`q` on the WebSocket, which
  `Viewer.explicit` carries); otherwise two devices with different `DEFAULT_QUALITY`
  would each download their own copy and hold two slots for three days.
- **Metadata is cached for an hour** (`INFO_TTL`), well inside the roughly six that
  YouTube's media URLs stay valid for. The download hands the whole dump back to yt-dlp
  via `--load-info-json`, so expired URLs in it fail the download outright rather than
  degrade — which is what the `library.forget` and re-extract in `Job.run` is for.
- **VLC elsewhere is told a URL and nothing more.** Its tvOS Remote Playback screen runs
  a web server whose page opens a WebSocket back to it and sends
  `{"type": "openURL", "url": ...}` — that is the whole protocol, and it has no field for
  a position. So `playlist_from` trims the HLS playlist to start at the wanted segment
  (keeping `#EXT-X-MAP`, which fMP4 cannot play without) and `/media/{token}/from-{n}.m3u8`
  serves it. That route sits beside `index.m3u8`, not below it, so the segment names
  inside resolve, and it must stay registered before the general media route, which would
  otherwise match the same path. The URL is built from the request's own `Host`, since
  only the browser knows which address reaches this server — which is also why `/api/cast`
  refuses a localhost one outright, and why `PASSWORD` has to travel inside that URL.
- **VLC skips nothing either, so the sponsors are cut out of its playlist.** The same
  `playlist_from` leaves out whole segments the way it leaves out the ones before a
  position, taking a segment whose time is more sponsor than video and marking the splice
  `#EXT-X-DISCONTINUITY`, since what follows carries the timestamp it always had. Segment
  granularity is about as fine as `-c copy` could cut anyway, and the files stay whole: the
  browser goes on skipping to the exact second with an Undo, positions in `Store` keep
  meaning what they did, and a community segment that turns out to be wrong costs nothing.
  Casting looks the ranges up and writes them to `sponsor.json` beside the segments, and
  only the route reads them, because VLC reloads an EVENT playlist every few seconds and
  the media path must never wait on a third party. An empty answer leaves the file alone,
  so an unreachable SponsorBlock cannot undo a lookup that worked.
- **Anything that changes state checks `_same_origin`**, the WebSocket and the row's
  `DELETE /api/videos/{id}/{quality}` alike: with `PASSWORD` unset there is nothing else
  standing between a stray web page and this server, and a form POST needs no preflight.
  An absent `Origin` header is refused, not waved through.
- **Auth is Basic plus a cookie, not either one.** Some browsers do not repeat Basic
  credentials on a WebSocket handshake, so the middleware sets a cookie (an HMAC of the
  password, compared with `compare_digest`) on the first authorized response. `/healthz`
  is exempt so the container's healthcheck needs no credentials.
- **Only the throbber is drawn over the video.** Everything the player has to say —
  `notice()` and `toast()` alike — lands in `#status` under the picture, on the line
  that carries the download progress. An embed is the exception: it has no bar, so
  `.embed` rules float those same two elements over the video, which is the only place
  left to put them.
- **`Jobs.remove` marks the job `expired` before deleting it**, which is what makes a
  page still showing that video say so rather than reconnect and download it again. The
  page's wording covers both causes, because it cannot tell aging-out from an x.
- **`fresh=1` on the WebSocket means a person did this.** Without it the server treats a
  connection as a socket that dropped on its own, and answers `expired` for a video past
  its window instead of re-downloading it for a tab forgotten for days. Set it for the
  initial connect, quality changes and the retry buttons; never for auto-reconnects.
- **`/watch` starts the extraction before the page's script has loaded**, but only for a
  real navigation: the `Sec-Fetch-Dest: document` check stops another site that loads the
  address as an image or a frame from setting a download going. `Library.prefetch` caps
  how many such speculative fetches may be in flight (`MAX_PREFETCH`).
- **yt-dlp is deliberately not in the image.** It is installed into the `/data` volume
  on first start. The `PYTHON_TAG` suffix in `updater.py` forces existing installs to
  rebuild when it changes.
- **yt-dlp and ffmpeg always run via `up.environment()`**, which puts the current
  venv's `bin` on `PATH` and strips `PASSWORD` from the child environment.
- **The browser must never talk to a third party.** The CSP is `self`-only, so assets
  cannot come from a CDN — hls.js is vendored into the image by `ADD --checksum` in the
  Dockerfile. Thumbnails and SponsorBlock are proxied through `/api/thumbnail/` and
  `/api/segments/` (the latter via the hash-prefix API, so SponsorBlock does not learn
  the exact video). `frame-ancestors` is the one directive that varies: `*` under
  `/embed/`, which is the point of that route, and `'self'` everywhere else.
- **The container is `read_only` with `cap_drop: [ALL]`** and runs as uid 1000. Only
  `/data` and a tmpfs `/tmp` are writable.
- **Video IDs are validated everywhere they enter** — `VIDEO_ID` in route patterns, in
  `parse_video_id`, and again on the WebSocket query — because they reach a subprocess
  argv. `/media/{token}/{name}` additionally restricts filenames to the HLS set.
- **Errors are viewer-facing.** `VideoError` carries text meant for the page;
  `error_message()` strips yt-dlp's advice aimed at CLI users. A failure triggers a
  yt-dlp update check and one retry, both in `Library._fetch` and `Job.run`.
- **Only single finished videos work.** Livestreams raise in `parse_info`; channels,
  playlists and searches fall through to the catch-all route and render the
  "unsupported" section. The URL shapes mirror Invidious so redirect extensions work
  unchanged.
- **Video descriptions are untrusted** and built from text nodes in `linkify()`, never
  as HTML.
