"""YouTube Downloader — standalone FastAPI app for Render + R2.

UI: ytdown.to style single page. Server: resolve + prepare (yt-dlp +
ffmpeg merge so every video has sound) + progress polling.

CRITICAL download rule (root-cause analysis): downloads go through the
same-origin route GET /download/{job_id}, which streams the prepared
file with Content-Disposition: attachment, exact Content-Length and
Accept-Ranges + Range support. The Download button is a plain
<a href="/download/{job_id}" download="..."> anchor. There is NO
fetch()/Blob()/URL.createObjectURL()/window.open()/target="_blank"
anywhere in the download path — save progress lives in the browser's
own download bar, exactly like ytdown.to.

Only download videos you own or have permission to download, for your
own offline use. Do not re-upload downloaded videos.
"""
import glob
import os
import re
import shutil
import subprocess
import urllib.request
import zipfile

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import jobs as jobmod
from . import store as store_mod
from . import ytdl

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
STATIC_DIR = os.path.join(BASE_DIR, "static")

MIME = {
    "mp4": "video/mp4", "mkv": "video/x-matroska",
    "mp3": "audio/mpeg", "m4a": "audio/mp4", "wav": "audio/wav",
    "srt": "text/plain", "vtt": "text/vtt",
    "jpg": "image/jpeg", "jpeg": "image/jpeg",
    "zip": "application/zip",
}

app = FastAPI(title="YouTube Downloader")
jobmod.start_sweeper()
jobmod.cleanup_orphans()


def _client_ip(request: Request):
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _cooldown_or_429(request: Request):
    wait = jobmod.check_cooldown(_client_ip(request))
    if wait:
        return JSONResponse(
            {"error": "cooldown",
             "detail": "Please wait %d seconds between downloads." % wait,
             "retry_after": wait}, status_code=429)
    return None


def _set_progress(job, prog):
    job.progress.update(prog.snapshot())


def _finish(job, key, filename, mime, size, record=True):
    job.key = key
    job.filename = filename
    job.mime = mime
    job.size = size
    job.status = "ready"
    job.progress.update({"stage": "Ready", "percent": 100.0,
                         "done_bytes": size, "total_bytes": size})
    if record:
        jobmod.record_history(job)


def _store_put(job, local_file, filename):
    st = store_mod.Store()
    key, size = st.put(job.id, filename, local_file)
    return key, size


# ---------------------------------------------------------------- resolve
@app.post("/api/resolve")
def api_resolve(payload: dict):
    url = (payload.get("url") or "").strip()
    if not url:
        return JSONResponse({"error": "missing url"}, status_code=400)
    try:
        return ytdl.resolve(url)
    except RuntimeError as ex:
        return JSONResponse({"error": str(ex)}, status_code=502)


# ---------------------------------------------------------------- prepare
def _video_fmt(height):
    h = int(height)
    if h <= 1080:
        # Prefer H.264 video + AAC audio first: the merged file is then
        # already universal, so conversion is a seconds-long remux
        # (+faststart) instead of a full re-encode — essential on the
        # free tier's 0.1 CPU, where a 5-minute re-encode never finishes.
        return ("bv*[height<=%d][vcodec^=avc1]+ba[acodec^=mp4a]/"
                "bv*[height<=%d][vcodec^=avc1]+ba/"
                "bv*[height<=%d]+ba/b[height<=%d]/bv*+ba/b"
                % (h, h, h, h)), "mp4"
    return "bv*[height<=%d]+ba/b[height<=%d]" % (h, h), "mkv"


def _prepare_video(job, url, height, record=True):
    meta = ytdl.resolve(url)
    if meta.get("type") != "video":
        raise RuntimeError("not a single video URL")
    title, vid = meta["title"], meta["id"]
    job.title, job.video_id = title, vid
    job.extra = {"height": height}
    st = store_mod.Store()
    workdir = st.local_path(job.id, "work")
    os.makedirs(workdir, exist_ok=True)
    fmt, merge_fmt = _video_fmt(height)
    prog = ytdl.Progress()
    prog.stage = "Downloading…"
    job.progress.update(prog.snapshot())
    raw = ytdl.download_raw(url, workdir, fmt, merge_fmt=merge_fmt,
                            progress=prog)
    _set_progress(job, prog)
    job.progress["stage"] = "Joining picture + sound…"
    ext = merge_fmt
    filename = ytdl.video_filename(vid, title, height, ext)
    final = os.path.join(workdir, filename)
    if int(height) <= 1080:
        ytdl.to_universal(raw, final, quality=str(height))
    else:
        # 4K/8K (VP9/AV1): keep the merged MKV, no re-encode.
        shutil.move(raw, final)
    if os.path.abspath(raw) != os.path.abspath(final) and os.path.exists(raw):
        try:
            os.remove(raw)
        except OSError:
            pass
    key, size = _store_put(job, final, filename)
    _finish(job, key, filename, MIME[ext], size, record=record)


@app.post("/api/prepare")
def api_prepare(payload: dict, request: Request):
    err = _cooldown_or_429(request)
    if err:
        return err
    url = (payload.get("url") or "").strip()
    height = int(payload.get("quality") or payload.get("height") or 1080)
    if not url:
        return JSONResponse({"error": "missing url"}, status_code=400)
    job = jobmod.create_job("video")
    jobmod.submit(lambda j: _prepare_video(j, url, height), job)
    return {"job_id": job.id, "status": "preparing"}


def _prepare_audio(job, url, fmt):
    meta = ytdl.resolve(url)
    title, vid = meta["title"], meta["id"]
    job.title, job.video_id = title, vid
    job.extra = {"format": fmt}
    st = store_mod.Store()
    workdir = st.local_path(job.id, "work")
    os.makedirs(workdir, exist_ok=True)
    prog = ytdl.Progress()
    prog.stage = "Downloading audio…"
    # Audio-only: no merge happens, so the merge format must be a valid
    # container ("m4a" is rejected by yt-dlp); conversion to the
    # requested format is done by ffmpeg afterwards.
    raw = ytdl.download_raw(url, workdir, "ba/b", merge_fmt="mp4",
                            progress=prog)
    _set_progress(job, prog)
    job.progress["stage"] = "Converting to %s…" % fmt.upper()
    filename = ytdl.branded_filename(vid, title, "audio", fmt, fmt)
    final = os.path.join(workdir, filename)
    ytdl.to_audio(raw, final, fmt)
    if os.path.exists(raw) and os.path.abspath(raw) != os.path.abspath(final):
        try:
            os.remove(raw)
        except OSError:
            pass
    key, size = _store_put(job, final, filename)
    _finish(job, key, filename, MIME[fmt], size)


@app.post("/api/prepare-audio")
def api_prepare_audio(payload: dict, request: Request):
    err = _cooldown_or_429(request)
    if err:
        return err
    url = (payload.get("url") or "").strip()
    fmt = (payload.get("format") or "mp3").lower()
    if fmt not in ("mp3", "m4a", "wav"):
        return JSONResponse({"error": "bad format"}, status_code=400)
    if not url:
        return JSONResponse({"error": "missing url"}, status_code=400)
    job = jobmod.create_job("audio")
    jobmod.submit(lambda j: _prepare_audio(j, url, fmt), job)
    return {"job_id": job.id, "status": "preparing"}


def _prepare_subtitle(job, url, lang, fmt):
    meta = ytdl.resolve(url)
    title, vid = meta["title"], meta["id"]
    job.title, job.video_id = title, vid
    job.extra = {"lang": lang, "format": fmt}
    st = store_mod.Store()
    workdir = st.local_path(job.id, "work")
    os.makedirs(workdir, exist_ok=True)
    job.progress["stage"] = "Fetching subtitles…"
    out = os.path.join(workdir, "sub.%(ext)s")
    args = ["--skip-download", "--write-subs", "--write-auto-subs",
            "--sub-langs", lang, "--sub-format", fmt,
            "--convert-subs", fmt, "-o", out, url]
    ok = False
    last_err = ""
    for client in ytdl.CLIENTS:
        p = ytdl.run_yt(args, client)
        if p.returncode == 0:
            ok = True
            break
        tail = (p.stderr or "").strip().splitlines()
        last_err = tail[-1] if tail else "subtitle download failed"
        if ytdl.is_rate_limited(last_err):
            raise RuntimeError("RATE_LIMITED: " + last_err)
    if not ok:
        raise RuntimeError("YT_BLOCKED: " + last_err)
    cands = [c for c in sorted(glob.glob(os.path.join(workdir, "sub.*")))
             if not c.endswith((".part", ".ytdl"))]
    if not cands:
        raise RuntimeError("no subtitles available for " + lang)
    src = cands[0]
    ext = fmt
    filename = ytdl.branded_filename(vid, title, "subtitles", lang, ext)
    final = os.path.join(workdir, filename)
    shutil.move(src, final)
    key, size = _store_put(job, final, filename)
    _finish(job, key, filename, MIME[ext], size)


@app.post("/api/prepare-subtitle")
def api_prepare_subtitle(payload: dict, request: Request):
    err = _cooldown_or_429(request)
    if err:
        return err
    url = (payload.get("url") or "").strip()
    lang = (payload.get("lang") or "en").strip()
    fmt = (payload.get("format") or "srt").lower()
    if fmt not in ("srt", "vtt"):
        return JSONResponse({"error": "bad format"}, status_code=400)
    if not url:
        return JSONResponse({"error": "missing url"}, status_code=400)
    job = jobmod.create_job("subtitle")
    jobmod.submit(lambda j: _prepare_subtitle(j, url, lang, fmt), job)
    return {"job_id": job.id, "status": "preparing"}


def _prepare_thumbnail(job, url):
    meta = ytdl.resolve(url)
    title, vid = meta["title"], meta["id"]
    job.title, job.video_id = title, vid
    thumb = meta.get("thumbnail")
    if not thumb:
        raise RuntimeError("no thumbnail found")
    job.progress["stage"] = "Downloading thumbnail…"
    st = store_mod.Store()
    workdir = st.local_path(job.id, "work")
    os.makedirs(workdir, exist_ok=True)
    filename = ytdl.branded_filename(vid, title, "thumbnail", "hd", "jpg")
    final = os.path.join(workdir, filename)
    req = urllib.request.Request(thumb, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r, open(final, "wb") as f:
        shutil.copyfileobj(r, f)
    if os.path.getsize(final) == 0:
        raise RuntimeError("thumbnail download empty")
    key, size = _store_put(job, final, filename)
    _finish(job, key, filename, MIME["jpg"], size)


@app.post("/api/prepare-thumbnail")
def api_prepare_thumbnail(payload: dict, request: Request):
    err = _cooldown_or_429(request)
    if err:
        return err
    url = (payload.get("url") or "").strip()
    if not url:
        return JSONResponse({"error": "missing url"}, status_code=400)
    job = jobmod.create_job("thumbnail")
    jobmod.submit(lambda j: _prepare_thumbnail(j, url), job)
    return {"job_id": job.id, "status": "preparing"}


def _prepare_playlist_zip(job, url, height, limit=None):
    meta = ytdl.resolve(url)
    if meta.get("type") != "playlist":
        raise RuntimeError("not a playlist URL")
    entries = meta.get("entries") or []
    if limit:
        entries = entries[:int(limit)]
    if not entries:
        raise RuntimeError("playlist is empty")
    job.title = meta.get("title")
    job.video_id = meta.get("id")
    job.extra = {"height": height, "count": len(entries)}
    st = store_mod.Store()
    workdir = st.local_path(job.id, "work")
    os.makedirs(workdir, exist_ok=True)
    total = len(entries)
    width = len(str(total))
    made = []
    for i, e in enumerate(entries, 1):
        job.progress.update({
            "stage": "Preparing video %d of %d…" % (i, total),
            "percent": round((i - 1) / total * 100.0, 1),
            "done_bytes": i - 1, "total_bytes": total})
        vurl = e.get("url")
        if not vurl:
            continue
        try:
            sub = jobmod.Job("video")
            _prepare_video(sub, vurl, height, record=False)
            # _prepare_video stored the file under sub.id — move it into
            # this job's zip staging area.
            staged = os.path.join(workdir, "%s - %s" % (
                str(i).zfill(max(2, width)),
                ytdl.safe_name(e.get("title") or ("video %d" % i)) + ".mp4"))
            if sub.key and os.path.exists(sub.key):
                shutil.move(sub.key, staged)
                made.append(staged)
            elif sub.key and st.use_r2:
                # R2 path: download back for zipping (small playlists only)
                import boto3
                obj = boto3.client(
                    "s3", endpoint_url=os.environ["R2_ENDPOINT"],
                    aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
                    aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
                    region_name="auto").get_object(
                        Bucket=os.environ["R2_BUCKET"], Key=sub.key)
                with open(staged, "wb") as f:
                    shutil.copyfileobj(obj["Body"], f)
                made.append(staged)
        except Exception as ex:  # noqa: BLE001 - one bad video must not kill the ZIP
            job.extra.setdefault("failed", []).append(
                {"n": i, "title": e.get("title"), "error": str(ex)[:200]})
    if not made:
        raise RuntimeError("no videos could be prepared for this playlist")
    job.progress.update({"stage": "Zipping…", "percent": 95.0})
    pid = meta.get("id") or "playlist"
    filename = ytdl.branded_filename(pid, meta.get("title") or "playlist",
                                     "playlist", "zip", "zip")
    zpath = os.path.join(workdir, filename)
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in made:
            zf.write(f, arcname=os.path.basename(f))
    for f in made:
        try:
            os.remove(f)
        except OSError:
            pass
    key, size = _store_put(job, zpath, filename)
    _finish(job, key, filename, MIME["zip"], size)


@app.post("/api/prepare-playlist")
def api_prepare_playlist(payload: dict, request: Request):
    err = _cooldown_or_429(request)
    if err:
        return err
    url = (payload.get("url") or "").strip()
    height = int(payload.get("quality") or payload.get("height") or 1080)
    limit = int(payload.get("limit") or 0) or None
    if not url:
        return JSONResponse({"error": "missing url"}, status_code=400)
    job = jobmod.create_job("zip")
    jobmod.submit(lambda j: _prepare_playlist_zip(j, url, height, limit), job)
    return {"job_id": job.id, "status": "preparing"}


@app.post("/api/batch")
def api_batch(payload: dict, request: Request):
    err = _cooldown_or_429(request)
    if err:
        return err
    urls = [u.strip() for u in (payload.get("urls") or []) if u.strip()]
    height = int(payload.get("quality") or payload.get("height") or 1080)
    if not urls:
        return JSONResponse({"error": "no urls"}, status_code=400)
    ids = []
    for url in urls[:20]:
        job = jobmod.create_job("video")
        jobmod.submit(lambda j, u=url: _prepare_video(j, u, height), job)
        ids.append(job.id)
    return {"job_ids": ids, "status": "preparing"}


# ---------------------------------------------------------------- progress
@app.get("/api/progress/{job_id}")
def api_progress(job_id: str):
    job = jobmod.get_job(job_id)
    if not job:
        return JSONResponse({"error": "unknown job"}, status_code=404)
    return job.to_dict()


@app.get("/api/history")
def api_history():
    return {"history": jobmod.get_history()}


@app.get("/healthz")
def healthz():
    return {"ok": True}


# ---------------------------------------------------------------- download
# THE critical route. Same-origin, attachment disposition, exact length,
# Range support. The Download button is a plain anchor to this URL —
# no fetch / Blob / object URL / window.open / target=_blank anywhere.
# HEAD is supported so `curl -I` can verify the headers without
# downloading the file.
@app.api_route("/download/{job_id}", methods=["GET", "HEAD"])
def download(job_id: str, request: Request):
    job = jobmod.get_job(job_id)
    if not job or job.status != "ready" or not job.key:
        return JSONResponse({"error": "file not ready"}, status_code=404)
    st = store_mod.Store()
    try:
        size = st.file_size(job.key)
    except Exception:
        return JSONResponse({"error": "file gone"}, status_code=404)
    filename = job.filename or "download"
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    mime = MIME.get(ext, "application/octet-stream")
    headers = {
        "Content-Disposition": 'attachment; filename="%s"' % filename,
        "Content-Type": mime,
        "Accept-Ranges": "bytes",
    }
    range_header = request.headers.get("range")
    start, end = 0, size - 1
    status = 200
    if range_header:
        m = re.match(r"bytes=(\d*)-(\d*)", range_header.strip())
        if m:
            s, e = m.group(1), m.group(2)
            start = int(s) if s else 0
            end = int(e) if e else size - 1
            if start >= size:
                return JSONResponse(
                    {"error": "range not satisfiable"}, status_code=416,
                    headers={"Content-Range": "bytes */%d" % size})
            end = min(end, size - 1)
            status = 206
            headers["Content-Range"] = "bytes %d-%d/%d" % (start, end, size)
    length = end - start + 1
    headers["Content-Length"] = str(length)
    if request.method == "HEAD":
        from fastapi import Response
        return Response(status_code=status, headers=headers)
    stream, _n = st.open_stream(job.key, start=start, end=end)
    return StreamingResponse(stream, status_code=status, headers=headers,
                             media_type=mime)


# ---------------------------------------------------------------- static UI
@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
