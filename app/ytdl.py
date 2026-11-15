"""yt-dlp core for the YouTube Downloader site.

Reuses the proven invocation pattern from ~/workspace/tools/yt-playlist-dl:
venv yt-dlp with --no-check-certificate, client ladder
default -> android -> ios -> tv -> web, universal H.264/yuv420p/AAC +
faststart output, --mp3 mode.

Only download videos you own or have permission to download, for your
own offline use. Do not re-upload downloaded videos.
"""
import glob
import json
import os
import re
import shutil
import subprocess
import unicodedata

CLIENTS = ["default", "android", "ios", "tv", "web"]
BLOCK_HINTS = ("sign in to confirm", "not a bot", "bot", "403", "forbidden",
               "timed out", "timeout", "unable to extract", "player response",
               "login required", "sign in")
RATE_HINTS = ("rate limit", "429", "too many requests", "ip address is blocked")

BRAND = "YTDownloader"
QUALITY_INDEX = {2160: "001", 1440: "002", 1080: "003", 720: "004",
                 480: "005", 360: "006", 240: "007", 144: "008"}
# Standard tiers, highest first. A rung whose true height is e.g. 808
# (1920x808) belongs to the 1080p tier — exactly how ytdown.to names
# its 1920x808 file _003_1080p.mp4.
TIERS = [2160, 1440, 1080, 720, 480, 360, 240, 144]


def _tier(height):
    # Smallest standard tier >= the true height: 808 -> 1080 (_003_),
    # 1078 -> 1080, 1616 -> 2160, 538 -> 720.
    for t in sorted(TIERS):
        if int(height) <= t:
            return t
    return 2160


def ytdlp_bin():
    env = os.environ.get("YTDLP_BIN")
    if env:
        return env
    venv = os.path.expanduser("~/workspace/venvs/ytdlp/bin/yt-dlp")
    if os.path.exists(venv):
        return venv
    found = shutil.which("yt-dlp")
    return found or "yt-dlp"


def _base_args(client="default"):
    cmd = [ytdlp_bin(), "--no-check-certificate", "--no-warnings",
           "--socket-timeout", "30"]
    if client != "default":
        cmd += ["--extractor-args", "youtube:player_client=" + client]
    return cmd


def run_yt(args, client="default"):
    return subprocess.run(_base_args(client) + args,
                          capture_output=True, text=True)


def is_blocked(text):
    return any(h in (text or "").lower() for h in BLOCK_HINTS)


def is_rate_limited(text):
    return any(h in (text or "").lower() for h in RATE_HINTS)


def get_json(url, flat=False, limit=None):
    """Metadata as JSON, trying the client ladder. Raises RuntimeError."""
    last_err = "yt-dlp failed"
    for client in CLIENTS:
        args = ["-J"]
        if flat:
            args += ["--flat-playlist"]
            if limit:
                args += ["--playlist-items", "1:" + str(limit)]
        p = run_yt(args + [url], client)
        if p.returncode == 0 and p.stdout.strip():
            try:
                return json.loads(p.stdout), client
            except json.JSONDecodeError:
                last_err = "bad JSON from yt-dlp (%s)" % client
                continue
        tail = (p.stderr or "").strip().splitlines()
        last_err = tail[-1] if tail else "yt-dlp failed (%s)" % client
        if is_rate_limited(last_err):
            raise RuntimeError("RATE_LIMITED: " + last_err)
    raise RuntimeError("YT_BLOCKED: " + last_err)


def safe_name(s, maxlen=120):
    s = re.sub(r'[\\/:*?"<>|]', "-", str(s or "untitled")).strip()
    s = re.sub(r"\s+", " ", s)
    return s[:maxlen].rstrip(" .") or "untitled"


def title_slug(title, maxlen=60):
    """ASCII hyphenated slug for branded filenames, ytdown.to style."""
    s = unicodedata.normalize("NFKD", str(title or "video"))
    s = s.encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-")
    if not s:
        s = "video"
    if len(s) > maxlen:
        s = s[:maxlen].rstrip("-")
    return s or "video"


def branded_filename(video_id, title, index_token, quality_token, ext):
    """YTDownloader_YouTube_Media_<id>_<slug>_<nnn>_<quality>.<ext>

    Mirrors ytdown.to's pattern (e.g. ..._003_1080p.mp4) with our own
    brand prefix. ASCII-only, so it is safe in Content-Disposition.
    """
    return "%s_YouTube_Media_%s_%s_%s_%s.%s" % (
        BRAND, str(video_id), title_slug(title), index_token,
        quality_token, ext)


def video_filename(video_id, title, height, ext):
    tier = _tier(height)
    return branded_filename(video_id, title, QUALITY_INDEX[tier],
                            "%dp" % tier, ext)


def probe(path):
    p = subprocess.run(["ffprobe", "-v", "quiet", "-print_format", "json",
                        "-show_streams", "-show_format", path],
                       capture_output=True, text=True)
    if p.returncode != 0:
        return None
    try:
        return json.loads(p.stdout)
    except json.JSONDecodeError:
        return None


def is_universal(path):
    """True if already H.264/yuv420p + AAC (or no audio)."""
    info = probe(path)
    if not info:
        return False
    v = a = None
    for s in info.get("streams", []):
        if s.get("codec_type") == "video" and v is None:
            v = s
        if s.get("codec_type") == "audio" and a is None:
            a = s
    if not v or v.get("codec_name") != "h264":
        return False
    if v.get("pix_fmt") != "yuv420p":
        return False
    if a and a.get("codec_name") != "aac":
        return False
    return True


def to_universal(src, dst, quality="1080", mp3=False):
    """Convert src -> dst as universal mp4 (or mp3). Returns dst."""
    if mp3:
        cmd = ["ffmpeg", "-y", "-v", "error", "-i", src,
               "-vn", "-c:a", "libmp3lame", "-q:a", "2", dst]
        p = subprocess.run(cmd, capture_output=True, text=True)
        if p.returncode != 0:
            raise RuntimeError("ffmpeg mp3 failed: " + (p.stderr or "").strip()[-300:])
        return dst
    if is_universal(src):
        cmd = ["ffmpeg", "-y", "-v", "error", "-i", src,
               "-c", "copy", "-movflags", "+faststart", dst]
        p = subprocess.run(cmd, capture_output=True, text=True)
        if p.returncode == 0:
            return dst
    max_h = {"1080": 1080, "720": 720, "480": 480, "360": 360,
             "best": 1080}.get(str(quality), 1080)
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", src,
           "-vf", "scale=-2:min(ih\\,%d)" % max_h,
           "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast",
           "-crf", "20", "-c:a", "aac", "-b:a", "128k",
           "-movflags", "+faststart", dst]
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError("ffmpeg convert failed: " + (p.stderr or "").strip()[-300:])
    return dst


def to_audio(src, dst, fmt):
    """Convert an audio-only download to mp3 / m4a / wav."""
    if fmt == "mp3":
        cmd = ["ffmpeg", "-y", "-v", "error", "-i", src,
               "-vn", "-c:a", "libmp3lame", "-q:a", "2", dst]
    elif fmt == "m4a":
        cmd = ["ffmpeg", "-y", "-v", "error", "-i", src,
               "-vn", "-c:a", "aac", "-b:a", "128k", dst]
    elif fmt == "wav":
        cmd = ["ffmpeg", "-y", "-v", "error", "-i", src,
               "-vn", "-c:a", "pcm_s16le", dst]
    else:
        raise RuntimeError("unsupported audio format: " + fmt)
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError("ffmpeg %s failed: %s" % (fmt, (p.stderr or "").strip()[-300:]))
    return dst


# ---------------------------------------------------------------- progress
_SIZE_RE = re.compile(r"([\d.]+)\s*([KMG]iB|B)")
_PROG_RE = re.compile(
    r"\[download\]\s+([\d.]+)% of\s+~?\s*([\d.]+[KMG]iB|Unknown)"
    r"(?:\s+at\s+([\d.]+[KMG]iB)/s)?(?:\s+ETA\s+(\d+:\d+))?")


def _size_bytes(s):
    m = _SIZE_RE.match(str(s).strip())
    if not m:
        return None
    val, unit = float(m.group(1)), m.group(2)
    return int(val * {"B": 1, "KiB": 1024, "MiB": 1024 ** 2,
                      "GiB": 1024 ** 3}[unit])


class Progress:
    """Aggregates yt-dlp download progress across video+audio streams."""

    def __init__(self):
        self.stage = "Preparing…"
        self.percent = 0.0
        self.done = 0
        self.total = None
        self.speed = ""
        self.eta = ""
        self._streams = []          # list of [done, total|None]
        self._current = None        # index into _streams

    def _recompute(self):
        done = sum(s[0] for s in self._streams)
        totals = [s[1] for s in self._streams if s[1]]
        total = sum(totals) if totals else None
        self.done = done
        self.total = total
        if total:
            self.percent = min(100.0, done / total * 100.0)

    def destination(self):
        self._streams.append([0, None])
        self._current = len(self._streams) - 1
        self._recompute()

    def line(self, text):
        if "[Merger] Merging formats" in text:
            self.stage = "Joining picture + sound…"
            if self.total:
                self.percent = max(self.percent, 99.0)
            return
        m = _PROG_RE.search(text)
        if not m:
            return
        try:
            pct = float(m.group(1))
        except ValueError:
            return
        total_s = m.group(2)
        total = None if total_s == "Unknown" else _size_bytes(total_s)
        speed = m.group(3) or ""
        eta = m.group(4) or ""
        if self._current is None:
            self._streams.append([0, None])
            self._current = 0
        if total:
            done = int(total * pct / 100.0)
            self._streams[self._current] = [done, total]
        else:
            # unknown total: keep percent from yt-dlp directly
            self.percent = pct
        self.speed = speed
        self.eta = eta
        self._recompute()

    def snapshot(self):
        return {"stage": self.stage, "percent": round(self.percent, 1),
                "done_bytes": self.done,
                "total_bytes": self.total,
                "speed": self.speed, "eta": self.eta}


def _run_with_progress(args, progress, on_line=None):
    """Run yt-dlp, feeding progress lines to a Progress object.

    Returns (returncode, last_error_line). Raises on rate-limit.
    """
    cmd = _base_args("default") + args
    last_err = ""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, bufsize=1)
    for line in proc.stdout:
        line = line.rstrip("\n")
        if "[download] Destination:" in line:
            progress.destination()
        elif line.startswith("[download]") or "[Merger]" in line:
            progress.line(line)
        if on_line:
            on_line(line)
        if line.strip():
            last_err = line.strip()
    proc.wait()
    if is_rate_limited(last_err):
        raise RuntimeError("RATE_LIMITED: " + last_err)
    return proc.returncode, last_err


def download_raw(url, outdir, fmt, merge_fmt="mp4", progress=None,
                 extra_args=None):
    """Download with yt-dlp (client ladder) into outdir/raw.%(ext)s.

    Returns the merged file path. Raises RuntimeError (YT_BLOCKED etc.).
    """
    os.makedirs(outdir, exist_ok=True)
    tmp = os.path.join(outdir, "raw.%(ext)s")
    args = ["-f", fmt, "--merge-output-format", merge_fmt,
            "--newline", "-o", tmp]
    if extra_args:
        args += extra_args
    args += [url]
    last_err = ""
    ok = False
    for client in CLIENTS:
        cmd = _base_args(client) + args
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, bufsize=1)
        for line in proc.stdout:
            line = line.rstrip("\n")
            if progress is not None:
                if "[download] Destination:" in line:
                    progress.destination()
                elif line.startswith("[download]") or "[Merger]" in line:
                    progress.line(line)
            if line.strip():
                last_err = line.strip()
        proc.wait()
        if proc.returncode == 0:
            ok = True
            break
        if is_rate_limited(last_err):
            raise RuntimeError("RATE_LIMITED: " + last_err)
    if not ok:
        raise RuntimeError("YT_BLOCKED: " + last_err)
    cands = sorted(glob.glob(os.path.join(outdir, "raw.*")))
    cands = [c for c in cands
             if not c.endswith((".part", ".ytdl", ".tmp", ".temp"))]
    if not cands:
        raise RuntimeError("yt-dlp finished but no file appeared")
    return cands[0]


# ---------------------------------------------------------------- resolve
def _fmt_date(yyyymmdd):
    s = str(yyyymmdd or "")
    if len(s) == 8 and s.isdigit():
        months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug",
                  "Sep", "Oct", "Nov", "Dec"]
        try:
            return "%s %d, %s" % (months[int(s[4:6]) - 1], int(s[6:8]), s[:4])
        except (ValueError, IndexError):
            return s
    return s


def _best_thumb(thumbs):
    if not thumbs:
        return None
    best = max(thumbs, key=lambda t: t.get("width") or 0)
    return best.get("url")


def _ladder(formats):
    """Per-height best video format, for the Media dropdown."""
    by_h = {}
    for f in formats or []:
        if f.get("vcodec") in (None, "none"):
            continue
        h = f.get("height")
        if not h:
            continue
        cur = by_h.get(h)
        score = _codec_score(f.get("vcodec") or "") * 1000 + (f.get("fps") or 0)
        if cur is None or score > cur[0]:
            by_h[h] = (score, f)
    out = []
    for h in sorted(by_h, reverse=True):
        f = by_h[h][1]
        w = f.get("width") or 0
        label = "MP4 - (%dx%d HD)" % (w, h) if h >= 720 else "MP4 - (%dx%d)" % (w, h)
        out.append({
            "height": h, "width": w, "label": label,
            "vcodec": f.get("vcodec"),
            "filesize": f.get("filesize") or f.get("filesize_approx"),
            "format_id": f.get("format_id"),
        })
    return out


def _codec_score(vcodec):
    v = (vcodec or "").lower()
    if v.startswith("avc1"):
        return 3
    if v.startswith("vp9"):
        return 2
    if v.startswith("av01"):
        return 1
    return 0


def resolve(url):
    """Metadata for a video or playlist URL. Returns a dict."""
    try:
        d, _client = get_json(url, flat=True, limit=100)
    except RuntimeError:
        raise
    if d.get("_type") == "playlist":
        entries = []
        for i, e in enumerate(d.get("entries") or [], 1):
            if not e:
                continue
            entries.append({
                "n": i, "id": e.get("id"), "title": e.get("title"),
                "duration": e.get("duration"),
                "url": ("https://www.youtube.com/watch?v=" + str(e["id"]))
                       if e.get("id") else None,
                "thumbnail": _best_thumb(e.get("thumbnails") or []),
            })
        return {"type": "playlist", "id": d.get("id"),
                "title": d.get("title"), "uploader": d.get("uploader"),
                "count": len(entries), "entries": entries,
                "thumbnail": _best_thumb(d.get("thumbnails") or [])}
    # single video: flat call may strip formats; retry full if needed
    if not d.get("formats"):
        d, _client = get_json(url)
    subs = []
    for lang, tracks in (d.get("subtitles") or {}).items():
        subs.append({"lang": lang, "manual": True,
                     "formats": [t.get("ext") for t in tracks or []
                                 if t.get("ext") in ("srt", "vtt")] or ["srt"]})
    for lang in (d.get("automatic_captions") or {}):
        if lang not in [s["lang"] for s in subs]:
            subs.append({"lang": lang, "manual": False, "formats": ["srt", "vtt"]})
    ladder = _ladder(d.get("formats") or [])
    heights = [f["height"] for f in ladder]
    # Default: highest H.264 (avc1) format at or below 1080p — the fast,
    # ytdown-equivalent choice (1920x808 for Diwanay Dil, ~95 MB).
    # (Exact 1080p often does not exist; this video's rungs are
    # 1616/1078/808/… and 1078 is VP9, not H.264.)
    avc = [f for f in ladder
           if (f.get("vcodec") or "").startswith("avc1")
           and f["height"] <= 1080]
    if avc:
        default_h = max(f["height"] for f in avc)
    elif 1080 in heights:
        default_h = 1080
    else:
        under = [h for h in heights if h <= 1080]
        default_h = max(under) if under else (max(heights) if heights else 1080)
    return {
        "type": "video", "id": d.get("id"), "title": d.get("title"),
        "uploader": d.get("uploader") or d.get("channel"),
        "channel": d.get("channel"),
        "duration": d.get("duration"),
        "view_count": d.get("view_count"), "like_count": d.get("like_count"),
        "upload_date": _fmt_date(d.get("upload_date")),
        "description": (d.get("description") or "")[:500],
        "thumbnail": _best_thumb(d.get("thumbnails") or []),
        "formats": ladder, "default_height": default_h,
        "subtitles": sorted(subs, key=lambda s: (not s["manual"], s["lang"])),
        "url": url,
    }
