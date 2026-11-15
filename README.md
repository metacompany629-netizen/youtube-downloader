# YouTube Downloader — standalone website (Render free + Cloudflare R2 free)

A ytdown.to-style YouTube downloader as a standalone website: paste a link,
pick a format, and the file downloads straight to your device. Every video
is prepared on the server with its sound joined in (yt-dlp + ffmpeg merge),
so downloads are never silent.

**Features:** with-sound MP4 up to 4K/8K (H.264 FHD default), MP3/M4A/WAV
audio, subtitles (SRT/VTT, all available languages), HD thumbnail download,
playlists (list in order + Download all as one ZIP), batch mode (several
links at once), live progress bar (%, MB done/total, MB/s, ETA, "Joining
picture + sound…" stage), recent downloads history, 60-second per-IP
cooldown between prepares.

**For personal use only:** download videos you own or have permission to
download. Do not re-upload downloaded videos.

## How downloads work (the important part)

A deep root-cause analysis of four failed attempts on the earlier version
showed the download must be a **same-origin** route. This app serves every
file through `GET /download/{job_id}`, which streams the prepared file with:

- `Content-Disposition: attachment; filename="YTDownloader_YouTube_Media_<id>_<slug>_003_1080p.mp4"`
- exact `Content-Length`, correct `Content-Type`
- `Accept-Ranges: bytes` + `Range` support (206 partial content)

The Download button is a plain `<a href="/download/{job_id}" download="…">`
anchor. There is **no** `fetch()` / `Blob` / `URL.createObjectURL()` /
`window.open()` / `target="_blank"` anywhere in the download path — the
browser itself streams the file to Downloads and shows progress in its own
download bar, exactly like ytdown.to. (Earlier attempts failed precisely
because of those mechanisms: cross-origin `download` attributes are ignored
by Chrome and navigate instead; fetching a ~95 MB file into page memory
stalls after ~1 KB.)

## Deploy — Render (free) + R2 (free)

### 1. Accounts (you create these yourself)
- **GitHub** — free, no card. Create a repo, e.g. `youtube-downloader`.
- **Render** — free, no card. https://render.com
- **Cloudflare** — free. Create an **R2 bucket** (e.g. `yt-downloader-files`).
  R2 free tier: 10 GB storage, zero egress fees. (Whether R2 asks for a
  payment method on file was unverified at research time — check at signup.)

### 2. Push this code to your GitHub repo
```bash
git init
git add .
git commit -m "YouTube Downloader"
git remote add origin https://github.com/<you>/youtube-downloader.git
git push -u origin main
```

### 3. Deploy on Render
1. Render dashboard → **New + → Web Service** → connect your GitHub repo.
2. Render detects `render.yaml` (Blueprint) — or set manually:
   - **Runtime:** Docker, **Plan:** Free
   - **Health check path:** `/healthz`
3. Add the R2 environment variables in the service's **Environment** tab:
   - `R2_ENDPOINT` — e.g. `https://<account-id>.r2.cloudflarestorage.com`
   - `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET`
   - Leave them unset to fall back to local disk (ephemeral on Render free —
     files disappear on restart; R2 is recommended).
4. Deploy. Your site is live at `https://youtube-downloader.onrender.com`
   (free Render address). First visit after 15 min idle takes ~1 min to wake.

### 4. Smoke test (do this before telling anyone it works)
1. Paste `https://www.youtube.com/watch?v=L1O-Gfahrw` (Diwanay Dil).
2. Default format should be MP4 1920x808 HD, ~95 MB.
3. Click **Download** — the page must NOT navigate; a file named
   `YTDownloader_YouTube_Media_L1O-Gfahrw_Diwanay-Dil-…_003_1080p.mp4`
   must appear in the browser's download tray, complete to 100%, and play
   with sound (H.264 + AAC, 4:45).
4. Also try one MP3, one subtitle (SRT), and the HD thumbnail.

### 5. Keep it personal
Do not publish or index the link. Prepared files auto-delete after 24 h.
A public YouTube downloader violates YouTube's ToS and can be taken down
on DMCA notice on any host — keeping it personal/unlisted lowers (does not
remove) that risk. YouTube also intermittently blocks cloud datacenter IPs,
so some videos may fail with bot-checks on some days; no free fix exists.

## Native-Python fallback (if Docker is not allowed on Render free)

If Render's free tier refuses Docker deploys, deploy the same code without
Docker: Render → **New Web Service → Python** (native environment), with
these settings instead of the Dockerfile:

- **Build command:**
  `pip install -r requirements.txt && sudo apt-get update && sudo apt-get install -y ffmpeg`
  (If `apt-get` is unavailable in the native environment, add a
  `build.sh` that downloads a static ffmpeg binary into the app dir and
  sets `PATH` — ffmpeg is required for merging.)
- **Start command:** `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
- Same env vars as above. Do **not** build both — use Docker first,
  native Python only if Docker is rejected.

## Do NOT add Cloudflare in front (yet)

No Cloudflare proxy/CDN in front of the site for now — that is a later step
after the site works (free plan, DNS/CDN only, **never** with the managed
challenge enabled, which would block the site's own requests). R2 is
Cloudflare *storage*, not a front proxy, and is part of the design above.

## Run locally
```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
COOLDOWN_SECONDS=0 .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
# open http://127.0.0.1:8000
```
Without R2 env vars, prepared files stay on local disk under `data/jobs/`
and auto-delete after 24 h. Set `YTDLP_BIN` to override the yt-dlp binary.

## Layout
```
app/main.py      FastAPI app + /download route + API
app/ytdl.py      yt-dlp wrapper (resolve, formats, progress, universal mp4)
app/jobs.py      background jobs, progress, history, 24h sweeper, cooldown
app/store.py     R2 (boto3) or local-disk storage
static/          index.html / style.css / app.js (ytdown.to-style UI)
Dockerfile       python:3.12-slim + ffmpeg + yt-dlp
render.yaml      Render Blueprint (free plan, docker runtime)
```
