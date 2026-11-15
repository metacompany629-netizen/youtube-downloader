# Test Report — YouTube Downloader standalone site

Tested locally on 2026-11-15 (VM, Python 3.12, ffmpeg 8.1.2,
yt-dlp 2026.10.26). Server run with `COOLDOWN_SECONDS=0` (cooldown
disabled for testing; logic unit-tested separately).

## Passed

| # | Test | Result |
|---|------|--------|
| 1 | Resolve Diwanay Dil (`L1O-Gfahrrw`) | PASS — title, channel "Coke Studio Pakistan", 285 s, 7.8M views, 27.5K likes, uploaded Nov 13, 2026; format ladder 1616/1078/808/538/360/270/180/128; subtitles incl. en, ur; **default height 808** (H.264 FHD) |
| 2 | Prepare default 808p with sound | PASS — ready in 113 s (download ~100 s + merge/remux ~4 s after the AAC-preference fix); **99,328,199 bytes** |
| 3 | `curl -I /download/{job}` headers | PASS — `HTTP/1.1 200 OK`, `Content-Disposition: attachment; filename="YTDownloader_YouTube_Media_L1O-Gfahrw_…_003_1080p.mp4"`, `Content-Type: video/mp4`, `Accept-Ranges: bytes`, `Content-Length: 99328199`. Range request → `206`, `Content-Range: bytes 0-99/99328199` |
| 4 | Full download via curl + ffprobe | PASS — 99,328,199 bytes, exact match; **H.264 1920x808 yuv420p + AAC 44.1 kHz, duration 284.8 s (4:45)**; faststart confirmed (moov at 36 before mdat at 224761) |
| 5 | MP3 prepare | PASS — ready in 42 s, 7,072,077 bytes, branded filename |
| 6 | M4A prepare | PASS — ready in 55 s, 4,674,697 bytes |
| 7 | WAV prepare | PASS — ready in 12 s, 54,675,830 bytes |
| 8 | Subtitle SRT (en) | PASS — ready in 18 s, 8,850 bytes, branded filename; HEAD on its download URL: 200, `Content-Type: text/plain`, `Content-Length: 8850` |
| 9 | HD thumbnail | PASS — ready in 8 s, 271,414 bytes, branded filename; HEAD: 200, `image/jpeg` |
| 10 | Playlist ZIP (Bulbulay S2, limit 2, 360p) | PASS — ready in 132 s, 79,364,914 bytes; ZIP contains `01 - AB HOGA TAMASHA! ….mp4` (24,069,509 B) + `02 - Masla Saara Roti Da ….mp4` (56,255,781 B); `testzip()` clean |
| 11 | Download-path grep | PASS — zero `createObjectURL` / `window.open` / `target="_blank"` in executable code (only in comments/docstrings); `fetch(` appears only in the JSON API helpers (`api()`/`apiGet()`); downloads are plain anchors |
| 12 | Branded filenames (unit) | PASS — 808 → `…_003_1080p.mp4`; 1078 → `…_003_1080p.mp4`; 1616 → `…_001_2160p.mkv`; 538 → `…_004_720p.mp4`; audio/subtitle/thumbnail/playlist patterns verified |
| 13 | Cooldown logic (unit) | PASS — first call 0 s, immediate second call 60 s wait, different IP 0 s, disabled (`COOLDOWN_SECONDS=0`) 0 s |
| 14 | Index page | PASS — `GET /` 200, contains "Paste a YouTube link", format select, download anchor, subtitles section |

## Bugs found during testing (all fixed)

1. **Default quality picked VP9 1078p instead of H.264 808p.** The
   ladder has no exact 1080 rung (1616/1078/808/…); the old logic took
   the highest rung ≤1080. Fixed: default = highest **avc1** rung ≤1080.
2. **Full re-encode took 10+ minutes** (killed at 122 MB output).
   Cause: best-audio selection picked Opus, so the merge was
   H.264+Opus → not universal → full re-encode. Fixed: format selector
   now prefers `ba[acodec^=mp4a]` (AAC) first, so the merge is already
   universal and conversion is a seconds-long remux. Prepare dropped
   from 10+ min to 113 s. (This also matters on Render free's 0.1 CPU,
   where a re-encode would never finish.)
3. **Branded filename index `_000_808p`.** The tier lookup iterated
   the descending tier list and always returned 2160. Fixed: smallest
   standard tier ≥ true height (808 → 1080 → `_003_1080p`).
4. **MP3 prepare failed** with `yt-dlp: error: invalid merge output
   format "m4a" given` — audio-only downloads take no merge format.
   Fixed: use `mp4` as the (ignored) merge format for audio jobs.
5. **`curl -I` on `/download` returned 405.** FastAPI GET routes do
   not serve HEAD here. Fixed: route now declares `GET` + `HEAD` and
   answers HEAD with headers only.

## NOT proven / not tested

- **Real-browser tray download.** All download verification above is
  via curl (headers, Range, full byte-identical download). No live
  Chrome was driven to click the Download button and watch the tray —
  the mechanism is the standard same-origin anchor + attachment
  disposition (the exact pattern ytdown.to uses), but the tray
  behaviour itself is **not** proven here. The README's smoke test
  must be run in a real browser after deploy.
- **Docker build.** `docker` is not installed on this VM, so the
  Dockerfile was **not** built locally. Render builds it on deploy;
  if the image build fails there, the README's native-Python fallback
  applies.
- **R2 storage path.** No R2 credentials exist, so all tests ran on
  the local-disk fallback. The R2 code path (boto3 upload / streamed
  ranged GET) is written but **untested against real R2**.
- **4K prepare end-to-end.** The 1616p (VP9 → MKV) path was not run
  (only the filename mapping was unit-checked). The ≤1080p path is
  the tested one.
- **Cooldown over HTTP.** Verified by unit test only, not with the
  server running at `COOLDOWN_SECONDS=60`.

## Test artefacts

All large test outputs were deleted after verification (99 MB video,
79 MB ZIP, 54 MB WAV, /tmp downloads). `data/` is empty; the site
directory is just code + the local `.venv` (gitignored).
