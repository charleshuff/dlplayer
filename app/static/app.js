"use strict";

const $ = (selector) => document.querySelector(selector);
const video = $("#video");
const embedded = location.pathname.startsWith("/embed/");
const params = new URLSearchParams(location.search);
const ID = /^[A-Za-z0-9_-]{11}$/;
const videoId = embedded ? location.pathname.split("/")[2] : params.get("v") || "";

const stored = {
  get(key) { try { return localStorage.getItem(key); } catch { return null; } },
  set(key, value) { try { localStorage.setItem(key, value); } catch { /* private mode */ } },
};

function parseTime(text) {
  if (!text) return 0;
  if (/^\d+s?$/.test(text)) return parseInt(text, 10);
  const m = /^(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s?)?$/.exec(text);
  return m ? (+m[1] || 0) * 3600 + (+m[2] || 0) * 60 + (+m[3] || 0) : 0;
}

function clock(seconds) {
  const s = Math.max(0, Math.floor(seconds));
  const tail = `${String(s % 60).padStart(2, "0")}`;
  return s >= 3600
    ? `${Math.floor(s / 3600)}:${String(Math.floor(s / 60) % 60).padStart(2, "0")}:${tail}`
    : `${Math.floor(s / 60)}:${tail}`;
}

/* ------------------------------------------------------------------ state */

let socket = null;
let reconnect = 0;
let hls = null;
let hasFrame = false;       // something other than the poster is showing
let src = null;             // playlist currently loaded
let meta = null;
let state = "idle";
let onDisk = 0;             // seconds available
let target = parseTime(params.get("t") || params.get("start")); // wanted but maybe not on disk yet
let autoplay = !embedded;
let lastReport = 0;
let segments = [];
let kept = new Set();       // segments the viewer chose to watch
let wantedQuality = stored.get("quality");
let skipping = stored.get("skip") !== "off";

const total = () => (state === "ready" && onDisk) || (meta && meta.duration) || onDisk;

function notice(text, action, run) {
  const box = $("#notice");
  box.textContent = text || "";
  if (action) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = action;
    button.addEventListener("click", run);
    box.append(button);
  }
}

let toastTimer = 0;

// A short-lived message with one way to undo whatever just happened.
function toast(text, action, run) {
  const box = $("#toast");
  const button = box.lastElementChild;
  box.firstElementChild.textContent = text;
  button.hidden = !action;
  if (action) {
    button.textContent = action;
    button.onclick = () => { box.hidden = true; run(); };
  }
  box.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { box.hidden = true; }, 6000);
}

// The throbber covers every wait in which there is nothing new to look at.
function drawBusy() {
  const busy = !["idle", "error", "expired"].includes(state) && (!src || !hasFrame || target > 0);
  $("#throbber").hidden = !busy;
  $("#progress .spin").hidden = state !== "downloading";
}
video.addEventListener("loadeddata", () => { hasFrame = true; drawBusy(); });

/* ------------------------------------------------------------- connection */

// `fresh` tells the server this is a deliberate visit rather than a socket that
// dropped on its own, so a tab forgotten for days does not download it again.
function connect(fresh = false) {
  clearTimeout(reconnect);
  if (socket) socket.onclose = socket.onmessage = null, socket.close();
  state = "connecting";
  drawBusy();
  if (!src) notice("Asking YouTube for the video");
  const query = new URLSearchParams({ v: videoId });
  if (fresh) query.set("fresh", "1");
  if (wantedQuality) query.set("q", wantedQuality);
  const ws = socket = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/api/ws?${query}`);
  ws.onmessage = (event) => update(JSON.parse(event.data));
  ws.onclose = () => {
    // Laptop slept, phone switched networks, server restarted. If the video
    // was deleted meanwhile, the server downloads it again.
    if (state === "error" || state === "expired") return;
    if (state === "connecting" && !src) notice("Lost contact with the server, retrying");
    reconnect = setTimeout(connect, 2000);
  };
}

function update(message) {
  apply(message);
  drawBusy();
}

function apply(message) {
  if (message.state) state = message.state;
  if (message.state === "installing") return notice("The server is installing yt-dlp, this takes a moment the first time");
  if (state === "expired") {
    if (src) target = video.currentTime || 0;
    unload();
    onDisk = 0;
    showProgress({ bytes: 0 });
    return notice("This video is not on the server any more", "Download again", () => connect(true));
  }
  if (message.meta) {
    showMeta(message.meta, message.quality);
    // Where the last viewing stopped, here or on another device. A ?t= in the
    // address, or a position already on screen, says more than it does.
    if (!src && !target && message.resume) target = message.resume;
    if (!src) notice(message.wait > 1 ? `YouTube holds this video back for ${message.wait} seconds` : "Starting download");
  }
  if (message.seconds !== undefined) {
    onDisk = message.seconds;
    showProgress(message);
  }
  if (state === "error") {
    if (hls) hls.stopLoad();
    target = Math.max(target, video.currentTime || 0);
    return notice(message.error, "Try again", () => { unload(); connect(true); });
  }
  if (message.src && message.src !== src) load(message.src);
  else if (src && target) seek(target);
}

/* --------------------------------------------------------------- playback */

function unload() {
  src = null;  // before destroying hls.js, whose teardown can fire `pause` at 0
  if (hls) hls.destroy();
  hls = null;
  hasFrame = false;
}

// Reporting the position is what lets another device carry on from here, and
// what keeps the video on disk: the server deletes what nobody plays.
function report() {
  if (!src || !socket || socket.readyState !== WebSocket.OPEN) return;
  lastReport = Date.now();
  socket.send(JSON.stringify({ t: Math.floor(video.currentTime || 0) }));
}

video.addEventListener("timeupdate", () => {
  if (!video.paused && Date.now() - lastReport >= 10000) report();
});
video.addEventListener("pause", report);

function available(time) {
  return state === "ready" || time < onDisk - 2;
}

function load(playlist) {
  // Reloading after a quality change or a lost connection keeps the position.
  if (src) {
    target = Math.max(target, video.currentTime || 0);
    autoplay = !video.paused;
    unload();
  }
  if (meta && meta.duration && target >= meta.duration) target = 0;
  if (!available(target)) return waitFor(target);
  src = playlist;
  notice("");
  const start = target;
  target = 0;
  if (window.Hls && Hls.isSupported()) {
    hls = new Hls({ startPosition: start, maxBufferLength: 60, backBufferLength: 120 });
    hls.on(Hls.Events.ERROR, (_, data) => {
      if (!data.fatal) return;
      if (data.type === Hls.ErrorTypes.MEDIA_ERROR) return hls.recoverMediaError();
      // Usually the files are gone because the connection dropped for a while.
      target = video.currentTime || 0;
      unload();
      connect();
    });
    hls.loadSource(playlist);
    hls.attachMedia(video);
  } else if (video.canPlayType("application/vnd.apple.mpegurl")) {
    video.src = playlist;
    video.addEventListener("loadedmetadata", () => { video.currentTime = start; }, { once: true });
  } else {
    return notice("This browser cannot play streamed video.");
  }
  if (autoplay) video.play().catch(() => { /* blocked by the browser: the controls are right there */ });
}

function waitFor(time) {
  target = time;
  notice(`Downloading up to ${clock(time)}`);
  drawBusy();
}

function seek(time) {
  time = Math.max(0, Math.min(time, total()));
  if (!src || !available(time)) return waitFor(time);
  if (target) notice("");
  if (!(time <= video.duration + 1)) {
    // On disk, but the player has not read the latest playlist yet and would
    // stop at the end it knows about. Loading it afresh is nearly instant.
    const playlist = src;
    autoplay = !video.paused;
    unload();
    target = time;
    load(playlist);
    return drawBusy();
  }
  target = 0;
  drawBusy();
  video.currentTime = time;
}

/* -------------------------------------------------------------- the reel */

const reel = $("#reel");

function showProgress(message) {
  const length = total();
  $("#disk").style.width = `${length ? Math.min(100, (onDisk / length) * 100) : 0}%`;
  const size = `${Math.round(message.bytes / 1e6)} MB on disk`;
  $("#progress span").textContent =
    state === "expired" ? "Not on disk"
    : state === "ready" ? `Fully downloaded, ${size}`
    : onDisk ? `Downloaded ${clock(onDisk)} of ${clock(length)}, ${size}`
    : "Starting download";
  drawHead();
  drawMarks();
}

function drawHead() {
  const length = total();
  if (!length) return;
  const at = target || video.currentTime || 0;
  $("#head").style.left = `${Math.min(100, (at / length) * 100)}%`;
  reel.setAttribute("aria-valuemax", Math.floor(length));
  reel.setAttribute("aria-valuenow", Math.floor(at));
  reel.setAttribute("aria-valuetext", `${clock(at)} of ${clock(length)}`);
}

function drawMarks() {
  const length = total();
  if (!length || !skipping) return $("#marks").replaceChildren();
  $("#marks").replaceChildren(...segments.map((s) => {
    const mark = document.createElement("i");
    mark.style.left = `${(s.start / length) * 100}%`;
    mark.style.width = `${((s.end - s.start) / length) * 100}%`;
    mark.title = NAMES[s.category] || s.category;
    return mark;
  }));
}

function seekTo(event) {
  const box = reel.getBoundingClientRect();
  seek(((event.clientX - box.left) / box.width) * total());
}
reel.addEventListener("pointerdown", (event) => { reel.setPointerCapture(event.pointerId); seekTo(event); });
reel.addEventListener("pointermove", (event) => { if (event.buttons) seekTo(event); });
video.addEventListener("timeupdate", drawHead);
video.addEventListener("seeking", drawHead);

/* ----------------------------------------------------------- SponsorBlock */

const NAMES = {
  sponsor: "Sponsor", selfpromo: "Self-promotion", interaction: "Subscribe reminder", intro: "Intro",
  outro: "Outro", preview: "Preview", music_offtopic: "Non-music section", filler: "Filler",
};
async function loadSegments() {
  try {
    segments = await (await fetch(`/api/segments/${videoId}`)).json();
    drawMarks();
  } catch { /* skipping is optional */ }
}

video.addEventListener("timeupdate", () => {
  if (!skipping || video.paused || video.seeking) return;
  const now = video.currentTime;
  const hit = segments.find((s) => !kept.has(s) && now >= s.start && now < s.end - 0.3);
  // Keep playing it until the part after it has been downloaded.
  if (!hit || !available(hit.end)) return;
  seek(hit.end);
  toast(`Skipped ${(NAMES[hit.category] || hit.category).toLowerCase()}`, "Undo", () => { kept.add(hit); seek(hit.start); });
});

$("#sponsor input").checked = skipping;
$("#sponsor input").addEventListener("change", (event) => {
  skipping = event.target.checked;
  stored.set("skip", skipping ? "on" : "off");
  drawMarks();
});

/* ------------------------------------------------------------- video info */

function showMeta(info, quality) {
  meta = info;
  document.title = info.title;
  $("#title").textContent = info.title;
  $("#channel").textContent = info.channel;
  $("#views").textContent = info.views == null ? "" : `${info.views.toLocaleString()} views`;
  $("#date").textContent = info.date ? new Date(`${info.date}T12:00`).toLocaleDateString(undefined, { dateStyle: "long" }) : "";
  $("#description").replaceChildren(...linkify(info.description));
  $("#about").hidden = false;
  const select = $("#quality select");
  select.replaceChildren(...info.qualities.map((q) => new Option(`${q}p`, q, false, q === quality)));
  $("#quality").hidden = false;
}

$("#quality select").addEventListener("change", (event) => {
  wantedQuality = event.target.value;
  stored.set("quality", wantedQuality);
  notice(`Switching to ${wantedQuality}p`);
  connect(true);
});

// Built from text nodes, never HTML: descriptions are untrusted.
function linkify(text) {
  const nodes = [];
  let last = 0;
  for (const m of text.matchAll(/https?:\/\/[^\s<>"]+[^\s<>".,;:!?)\]']|(?<![\d:])(?:\d{1,2}:)?\d{1,2}:\d{2}(?![\d:])/g)) {
    nodes.push(text.slice(last, m.index));
    last = m.index + m[0].length;
    const a = document.createElement("a");
    a.textContent = m[0];
    if (m[0].startsWith("http")) {
      a.href = m[0];
      a.rel = "noreferrer noopener";
      // Links to other videos stay on this server.
      if (/^https?:\/\/((www|m)\.)?(youtube\.com\/(watch\?|shorts\/|live\/)|youtu\.be\/)/.test(m[0])) {
        a.href = `/go?url=${encodeURIComponent(m[0])}`;
      } else a.target = "_blank";
    } else {
      const seconds = m[0].split(":").reduce((sum, part) => sum * 60 + +part, 0);
      a.href = `/watch?v=${videoId}&t=${seconds}`;
      a.addEventListener("click", (event) => {
        event.preventDefault();
        seek(seconds);
        video.scrollIntoView({ block: "nearest" });
      });
    }
    nodes.push(a);
  }
  nodes.push(text.slice(last));
  return nodes;
}

/* --------------------------------------------------------------- keyboard */

addEventListener("keydown", (event) => {
  if (!src || event.ctrlKey || event.metaKey || event.altKey) return;
  if (/^(INPUT|SELECT|BUTTON|A|VIDEO)$/.test(event.target.tagName)) return;
  const jump = { j: -10, l: 10, ArrowLeft: -5, ArrowRight: 5 }[event.key];
  if (jump) seek(video.currentTime + jump);
  else if (event.key === " " || event.key === "k") video.paused ? video.play() : video.pause();
  else if (event.key === "m") video.muted = !video.muted;
  else if (event.key === "f") document.fullscreenElement ? document.exitFullscreen() : video.requestFullscreen();
  else return;
  event.preventDefault();
});

/* -------------------------------------------------------- to VLC */

// The same cone the home page's rows use, from the one template.
$("#cast").append($("#cone").content.firstElementChild.cloneNode(true));

// What came back from /api/cast, in one line. The rows below say it too.
function playing(answer) {
  return `Playing on VLC from ${clock(answer.at)}${answer.skipped ? ", sponsors left out" : ""}`;
}

// VLC's remote playback takes a URL and nothing else, so the position travels
// in the URL: a playlist that starts there. See app/cast.py.
$("#cast").addEventListener("click", async () => {
  const at = Math.floor(target || video.currentTime || 0);
  video.pause();
  toast(`Sending to VLC from ${clock(at)}`);
  try {
    const answer = await fetch("/api/cast", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ v: videoId, t: at }),
    });
    const said = await answer.json().catch(() => ({}));
    toast(answer.ok ? playing(said) : said.error || "VLC could not be reached");
  } catch {
    toast("The server could not be reached");
  }
});

/* -------------------------------------------------------- the home list */

const rows = new Map();     // video and quality -> the row showing it
let castable = false;       // whether this server has somewhere to send a video
let saidTimer = 0;

// The player's toast sits over the video, which there is none of here.
function said(text) {
  $("#sent").textContent = text;
  clearTimeout(saidTimer);
  saidTimer = setTimeout(() => { $("#sent").textContent = ""; }, 6000);
}

function megabytes(bytes) {
  return bytes >= 1e9 ? `${(bytes / 1e9).toFixed(1)} GB` : `${Math.round(bytes / 1e6)} MB`;
}

function makeRow() {
  const row = document.createElement("li");
  const link = document.createElement("a");
  for (const part of ["name", "meta", "bar"]) {
    const span = document.createElement("span");
    span.className = part;
    if (part === "bar") span.append(document.createElement("i"));
    link.append(span);
  }
  const actions = document.createElement("span");
  actions.className = "actions";
  for (const [part, label] of [["remove", "\u00d7"], ["send", ""]]) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = part;
    if (label) button.textContent = label;
    else button.append($("#cone").content.firstElementChild.cloneNode(true));
    actions.append(button);
  }
  row.append(link, actions);  // siblings: a button inside the link would not work
  return row;
}

async function sendToVlc(video) {
  said("Sending to VLC\u2026");
  try {
    const answer = await fetch("/api/cast", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ v: video.id, t: Math.floor(video.watched || 0) }),
    });
    const told = await answer.json().catch(() => ({}));
    said(answer.ok ? playing(told) : told.error || "VLC could not be reached");
  } catch {
    said("The server could not be reached");
  }
}

async function removeVideo(video) {
  try {
    await fetch(`/api/videos/${video.id}/${video.quality}`, { method: "DELETE" });
  } catch { /* the reload below shows whether it went */ }
  loadVideos();
}

// Rows are kept and updated rather than rebuilt, so that polling does not pull
// the list out from under a pointer or move the focus somewhere else.
function showVideos({ videos, bytes, free }) {
  const list = $("#downloads");
  $("#library").hidden = !videos.length;
  document.body.classList.toggle("stocked", videos.length > 0);
  $("#total").textContent = videos.length
    ? `${videos.length} video${videos.length === 1 ? "" : "s"} · ${megabytes(bytes)} on disk · ${megabytes(free)} free`
    : "";
  const shown = new Set();
  videos.forEach((video, i) => {
    const key = `${video.id} ${video.quality}`;
    shown.add(key);
    let row = rows.get(key);
    if (!row) rows.set(key, (row = makeRow()));
    const [link, actions] = row.children;
    const [name, meta, bar] = link.children;
    const [remove, send] = actions.children;
    link.href = `/watch?v=${video.id}`;
    remove.onclick = () => removeVideo(video);
    send.onclick = () => sendToVlc(video);
    send.hidden = !castable;
    // All from YouTube, so text nodes and never HTML.
    name.textContent = video.channel ? `${video.channel} – ${video.title}` : video.title;
    const length = video.duration || video.seconds;
    const watched = Math.min(video.watched || 0, length);
    meta.textContent = [
      `${video.quality}p`,
      watched >= 1 ? `${clock(watched)} of ${clock(length)}` : clock(length),
      megabytes(video.bytes),
      ...(video.state === "ready" ? [] : ["still downloading"]),
    ].join(" · ");
    // How far it has been played, which is what the bar shows.
    bar.firstElementChild.style.width = `${length ? Math.min(100, (watched / length) * 100) : 0}%`;
    remove.setAttribute("aria-label", `Delete ${name.textContent}`);
    send.setAttribute("aria-label", `Send ${name.textContent} to VLC`);
    send.title = "Send to VLC";
    if (list.children[i] !== row) list.insertBefore(row, list.children[i] || null);
  });
  for (const [key, row] of rows) {
    if (!shown.has(key)) {
      row.remove();
      rows.delete(key);
    }
  }
}

async function loadVideos() {
  try {
    showVideos(await (await fetch("/api/videos")).json());
  } catch { /* the next pass tries again */ }
}

/* ------------------------------------------------------------------ start */

function watch() {
  connect(true);
  loadSegments();
}

if (ID.test(videoId) && (embedded || location.pathname === "/watch")) {
  $("#player").hidden = false;
  // Nothing to paste while watching; the home icon below the video goes back.
  // The bar's space stays, though -- see style.css.
  document.body.classList.add("watching");
  video.poster = `/api/thumbnail/${videoId}`;
  fetch("/api/config").then((r) => r.json()).then((c) => {
    $("#sponsor").hidden = !c.sponsorblock;
    $("#cast").hidden = !c.vlc;
  }).catch(() => {});
  // Opened at the position being watched here.
  const link = $("#options .youtube");
  link.append($("#play").content.firstElementChild.cloneNode(true));
  link.href = `https://www.youtube.com/watch?v=${videoId}`;
  link.addEventListener("click", () => {
    const at = Math.floor(target || video.currentTime || 0);
    link.href = `https://www.youtube.com/watch?v=${videoId}${at > 5 ? `&t=${at}s` : ""}`;
    video.pause();
  });
  if (embedded) {
    // Downloads as soon as it scrolls into view, so a page with ten embeds
    // does not fetch ten videos nobody looks at.
    document.body.classList.add("embed");
    $("#throbber").hidden = true;
    new IntersectionObserver((entries, observer) => {
      if (!entries.some((e) => e.isIntersecting)) return;
      observer.disconnect();
      watch();
    }).observe($("#stage"));
  } else {
    watch();
  }
  // Leaving hands over the position; close() flushes it before the socket goes.
  addEventListener("pagehide", () => { clearTimeout(reconnect); report(); if (socket) { socket.onclose = null; socket.close(); } });
  addEventListener("pageshow", (event) => { if (event.persisted && src) connect(); });
} else if (location.pathname === "/") {
  $("#home").hidden = false;
  $("#origin").textContent = location.origin;
  $("#url").focus();
  loadVideos();
  // A progress bar that does not move is worse than none at all.
  setInterval(() => { if (!document.hidden) loadVideos(); }, 2000);
  // Pasting a link is enough, no need to press Watch.
  $("#url").addEventListener("paste", () => setTimeout(() => {
    if (/(youtube(-nocookie)?\.com\/|youtu\.be\/).*[\w-]{11}|^\s*https?:.*[?&]v=[\w-]{11}/.test($("#url").value)) $("form").requestSubmit();
  }));
  fetch("/api/config").then((r) => r.json()).then((c) => {
    castable = c.vlc;
    $("#version").textContent = c.ytDlp ? `yt-dlp ${c.ytDlp}` : "yt-dlp is still being installed";
    loadVideos();  // again, now that it is known whether the cone belongs on a row
  }).catch(() => {});
} else {
  $("#unsupported").hidden = false;
  if (location.pathname === "/go") $("#unsupported .youtube").hidden = true;
  else $("#unsupported .youtube").href = `https://www.youtube.com${location.pathname}${location.search}`;
}
