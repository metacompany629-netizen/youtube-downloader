/* YouTube Downloader — front end.
 *
 * NOTE ON DOWNLOADS: fetch() in this file is used ONLY for JSON API
 * calls (resolve / prepare / progress / history). A file download NEVER
 * goes through fetch, Blob, object URLs, window.open or a new tab:
 * every Download button is a plain <a href="/download/{job_id}"
 * download="filename"> anchor, and the browser itself streams the file
 * to Downloads with native progress in its download bar.
 */
"use strict";

const $ = (id) => document.getElementById(id);

function fmtBytes(n) {
  if (n == null) return "—";
  if (n < 1024) return n + " B";
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
  if (n < 1024 * 1024 * 1024) return (n / 1048576).toFixed(1) + " MB";
  return (n / 1073741824).toFixed(2) + " GB";
}
function fmtDur(s) {
  if (s == null) return "—";
  const m = Math.floor(s / 60), sec = Math.floor(s % 60);
  return m + ":" + String(sec).padStart(2, "0");
}
function fmtCount(n) {
  if (n == null) return "—";
  if (n >= 1e6) return (n / 1e6).toFixed(1) + "M";
  if (n >= 1e3) return (n / 1e3).toFixed(1) + "K";
  return String(n);
}
function showError(msg) {
  const el = $("error");
  el.textContent = msg;
  el.hidden = false;
}
function clearError() { $("error").hidden = true; }

/* JSON API helper — the ONLY place fetch() is used. */
async function api(path, body) {
  const r = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || data.error || ("HTTP " + r.status));
  return data;
}
async function apiGet(path) {
  const r = await fetch(path);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || ("HTTP " + r.status));
  return data;
}

let currentMeta = null;
let currentVideoJob = null;
let pollTimer = null;

function setBar(barEl, lineEl, pct, line) {
  const fill = barEl.querySelector("i");
  if (fill) fill.style.width = Math.max(0, Math.min(100, pct)) + "%";
  if (lineEl) lineEl.textContent = line || "";
}

function pollJob(jobId, barWrap, barEl, lineEl, onReady, onError) {
  const tick = async () => {
    let job;
    try {
      job = await apiGet("/api/progress/" + jobId);
    } catch (e) {
      clearInterval(pollTimer);
      if (onError) onError(e.message);
      return;
    }
    const p = job.progress || {};
    const line = (p.stage || "") + " · " +
      fmtBytes(p.done_bytes) + " of " + fmtBytes(p.total_bytes) +
      (p.speed ? " · " + p.speed + "/s" : "") +
      (p.eta ? " · ETA " + p.eta : "");
    if (barWrap) barWrap.hidden = false;
    setBar(barEl, lineEl, p.percent || 0, line);
    if (job.status === "ready") {
      clearInterval(pollTimer);
      onReady(job);
    } else if (job.status === "error") {
      clearInterval(pollTimer);
      if (onError) onError(job.error || "preparation failed");
    }
  };
  clearInterval(pollTimer);
  pollTimer = setInterval(tick, 1500);
  tick();
}

/* ------------------------------------------------------------------ go */
$("go-btn").addEventListener("click", async () => {
  clearError();
  const raw = $("url-input").value.trim();
  if (!raw) { showError("Paste a YouTube link first."); return; }
  const lines = raw.split(/\n+/).map((s) => s.trim()).filter(Boolean);
  if (lines.length > 1) { showBatch(lines); return; }
  $("result").hidden = true;
  $("playlist-box").hidden = true;
  $("batch-box").hidden = true;
  let meta;
  try {
    meta = await api("/api/resolve", { url: lines[0] });
  } catch (e) {
    showError(e.message);
    return;
  }
  if (meta.type === "playlist") { renderPlaylist(meta); return; }
  currentMeta = meta;
  renderVideo(meta);
  startVideoPrepare(meta.default_height);
});

$("paste-btn").addEventListener("click", async () => {
  try {
    const t = await navigator.clipboard.readText();
    if (t) $("url-input").value = t.trim();
  } catch (e) { /* clipboard unavailable — user can paste manually */ }
});

/* ------------------------------------------------------------- video */
function renderVideo(meta) {
  $("result").hidden = false;
  $("info-thumb").src = meta.thumbnail || "";
  $("info-channel").textContent = meta.uploader || meta.channel || "";
  $("info-title").textContent = meta.title || "";
  $("info-duration").textContent = fmtDur(meta.duration);
  $("info-views").textContent = fmtCount(meta.view_count);
  $("info-likes").textContent = fmtCount(meta.like_count);
  $("info-uploaded").textContent = meta.upload_date || "—";

  const sel = $("format-select");
  sel.innerHTML = "";
  (meta.formats || []).forEach((f) => {
    const o = document.createElement("option");
    o.value = f.height;
    o.textContent = f.label;
    if (f.height === meta.default_height) o.selected = true;
    sel.appendChild(o);
  });
  updateFileSize();
  sel.onchange = () => { updateFileSize(); startVideoPrepare(parseInt(sel.value, 10)); };

  const lang = $("sub-lang");
  lang.innerHTML = "";
  (meta.subtitles || []).forEach((s) => {
    const o = document.createElement("option");
    o.value = s.lang;
    o.textContent = s.lang + (s.manual ? "" : " (auto)");
    lang.appendChild(o);
  });
  if (!meta.subtitles || !meta.subtitles.length) {
    const o = document.createElement("option");
    o.value = "en"; o.textContent = "en";
    lang.appendChild(o);
  }
  $("thumb-link").hidden = true;
  $("sub-link").hidden = true;
  $("audio-link").hidden = true;
  $("download-note").textContent = "";
  const dl = $("download-btn");
  dl.setAttribute("aria-disabled", "true");
  dl.removeAttribute("href");
  dl.removeAttribute("download");
}

function updateFileSize() {
  const h = parseInt($("format-select").value, 10);
  const f = (currentMeta.formats || []).find((x) => x.height === h);
  $("file-size").textContent = f && f.filesize ? "≈ " + fmtBytes(f.filesize) : "—";
}

function startVideoPrepare(height) {
  if (!currentMeta) return;
  const dl = $("download-btn");
  dl.setAttribute("aria-disabled", "true");
  dl.removeAttribute("href");
  dl.removeAttribute("download");
  $("prepare-progress").hidden = false;
  api("/api/prepare", { url: currentMeta.url, quality: height })
    .then((res) => {
      currentVideoJob = res.job_id;
      pollJob(res.job_id, $("prepare-progress"), $("prepare-bar"), $("prepare-line"),
        (job) => {
          dl.href = "/download/" + job.job_id;
          dl.setAttribute("download", job.filename);
          dl.removeAttribute("aria-disabled");
          $("file-size").textContent = fmtBytes(job.size);
          $("download-note").textContent = job.filename + " · " + fmtBytes(job.size) +
            " · ready — tap Download and the file saves to your device.";
          $("prepare-progress").hidden = true;
          loadHistory();
        },
        (msg) => { showError(msg); $("prepare-progress").hidden = true; });
    })
    .catch((e) => { showError(e.message); $("prepare-progress").hidden = true; });
}

/* ---------------------------------------------------------- thumbnail */
$("thumb-btn").addEventListener("click", async () => {
  if (!currentMeta) return;
  let res;
  try { res = await api("/api/prepare-thumbnail", { url: currentMeta.url }); }
  catch (e) { showError(e.message); return; }
  pollJob(res.job_id, null, $("prepare-bar"), $("prepare-line"),
    (job) => {
      const a = $("thumb-link");
      a.href = "/download/" + job.job_id;
      a.setAttribute("download", job.filename);
      a.hidden = false;
      loadHistory();
    },
    (msg) => showError(msg));
});

/* ---------------------------------------------------------- subtitles */
let subFmt = "srt";
$("sub-srt").addEventListener("click", () => setSubFmt("srt"));
$("sub-vtt").addEventListener("click", () => setSubFmt("vtt"));
function setSubFmt(f) {
  subFmt = f;
  $("sub-srt").classList.toggle("active", f === "srt");
  $("sub-vtt").classList.toggle("active", f === "vtt");
  $("sub-btn").textContent = "⬇ Download subtitles (" + f.toUpperCase() + ")";
}
$("sub-btn").addEventListener("click", async () => {
  if (!currentMeta) return;
  let res;
  try {
    res = await api("/api/prepare-subtitle", {
      url: currentMeta.url, lang: $("sub-lang").value, format: subFmt });
  } catch (e) { showError(e.message); return; }
  pollJob(res.job_id, null, $("prepare-bar"), $("prepare-line"),
    (job) => {
      const a = $("sub-link");
      a.href = "/download/" + job.job_id;
      a.setAttribute("download", job.filename);
      a.hidden = false;
      loadHistory();
    },
    (msg) => showError(msg));
});

/* --------------------------------------------------------------- audio */
document.querySelectorAll(".audio-btn").forEach((btn) => {
  btn.addEventListener("click", async () => {
    if (!currentMeta) return;
    const fmt = btn.getAttribute("data-fmt");
    let res;
    try { res = await api("/api/prepare-audio", { url: currentMeta.url, format: fmt }); }
    catch (e) { showError(e.message); return; }
    pollJob(res.job_id, null, $("prepare-bar"), $("prepare-line"),
      (job) => {
        const a = $("audio-link");
        a.href = "/download/" + job.job_id;
        a.setAttribute("download", job.filename);
        a.hidden = false;
        loadHistory();
      },
      (msg) => showError(msg));
  });
});

/* ------------------------------------------------------------ playlist */
function renderPlaylist(meta) {
  $("playlist-box").hidden = false;
  $("playlist-title").textContent = (meta.title || "Playlist") + " · " + meta.count + " videos";
  const sel = $("playlist-quality");
  sel.innerHTML = "";
  [1080, 720, 480, 360].forEach((h) => {
    const o = document.createElement("option");
    o.value = h; o.textContent = h + "p";
    if (h === 1080) o.selected = true;
    sel.appendChild(o);
  });
  const ol = $("playlist-entries");
  ol.innerHTML = "";
  (meta.entries || []).forEach((e) => {
    const li = document.createElement("li");
    li.textContent = (e.title || e.id || "") + (e.duration ? " · " + fmtDur(e.duration) : "");
    ol.appendChild(li);
  });
  $("playlist-link").hidden = true;
  $("playlist-box").dataset.url = meta.url || "";
  $("playlist-box").dataset.id = meta.id || "";
  // keep the playlist url for the ZIP request
  $("playlist-zip-btn").onclick = async () => {
    let res;
    try {
      res = await api("/api/prepare-playlist", {
        url: $("url-input").value.trim(),
        quality: parseInt(sel.value, 10),
        limit: parseInt($("playlist-limit").value, 10) || 0 });
    } catch (e) { showError(e.message); return; }
    pollJob(res.job_id, $("playlist-progress"), $("playlist-bar"), $("playlist-line"),
      (job) => {
        const a = $("playlist-link");
        a.href = "/download/" + job.job_id;
        a.setAttribute("download", job.filename);
        a.hidden = false;
        $("playlist-progress").hidden = true;
        loadHistory();
      },
      (msg) => { showError(msg); $("playlist-progress").hidden = true; });
  };
}

/* --------------------------------------------------------------- batch */
function showBatch(lines) {
  $("batch-box").hidden = false;
  $("result").hidden = true;
  $("playlist-box").hidden = true;
  const sel = $("batch-quality");
  sel.innerHTML = "";
  [1080, 720, 480, 360].forEach((h) => {
    const o = document.createElement("option");
    o.value = h; o.textContent = h + "p";
    if (h === 1080) o.selected = true;
    sel.appendChild(o);
  });
  $("batch-go").onclick = async () => {
    let res;
    try {
      res = await api("/api/batch", {
        urls: lines, quality: parseInt(sel.value, 10) });
    } catch (e) { showError(e.message); return; }
    const ul = $("batch-list");
    ul.innerHTML = "";
    (res.job_ids || []).forEach((jobId, i) => {
      const li = document.createElement("li");
      li.innerHTML = "<span>" + (lines[i] || jobId) + "</span>" +
        "<span class='muted mono' id='batch-" + jobId + "'>preparing…</span>";
      ul.appendChild(li);
      const tick = async () => {
        let job;
        try { job = await apiGet("/api/progress/" + jobId); }
        catch (e) { return; }
        const el = document.getElementById("batch-" + jobId);
        if (!el) return;
        if (job.status === "ready") {
          el.innerHTML = "";
          const a = document.createElement("a");
          a.href = "/download/" + job.job_id;
          a.setAttribute("download", job.filename);
          a.textContent = "Download · " + fmtBytes(job.size);
          el.appendChild(a);
          loadHistory();
        } else if (job.status === "error") {
          el.textContent = job.error || "failed";
        } else {
          const p = job.progress || {};
          el.textContent = (p.stage || "preparing…") + " " + (p.percent || 0) + "%";
          setTimeout(tick, 1500);
        }
      };
      tick();
    });
  };
}

/* ------------------------------------------------------------- history */
async function loadHistory() {
  let data;
  try { data = await apiGet("/api/history"); } catch (e) { return; }
  const ul = $("history-list");
  ul.innerHTML = "";
  (data.history || []).forEach((it) => {
    const li = document.createElement("li");
    const span = document.createElement("span");
    span.textContent = (it.title || it.filename || it.job_id) + " · " + fmtBytes(it.size);
    li.appendChild(span);
    if (it.available) {
      const a = document.createElement("a");
      a.href = "/download/" + it.job_id;
      a.setAttribute("download", it.filename || "");
      a.textContent = "Download again";
      li.appendChild(a);
    } else {
      const s = document.createElement("span");
      s.className = "muted";
      s.textContent = "expired";
      li.appendChild(s);
    }
    ul.appendChild(li);
  });
}
loadHistory();
