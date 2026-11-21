import contextlib
import json
import os
import re
import subprocess
import time
from pathlib import Path

import numpy as np
import soundfile as sf

from . import config

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# ---------------------------------------------------------------- when YouTube refuses this server
# YouTube sometimes refuses a server's downloads altogether ("sign in to confirm you're not a bot"). It is a
# refusal of the server, not of one video, so once it happens YouTube links are turned away at once for a few
# minutes, with a clear message, instead of each one waiting to fail. Then the next link simply tries again.
YT_PAUSE = 5
_yt_refused_until = 0.0
YT_BLOCKED = ("YouTube isn't letting PeakCut download videos right now. Upload the video file instead, or use a "
              "Twitch or Kick link. YouTube links will be tried again in a few moments.")
_BOT_CHECK = re.compile(r"not a bot|confirm you.?re not|sign in to confirm(?! your age)", re.I)


class YouTubeBlocked(RuntimeError):
    pass


def is_youtube(url: str) -> bool:
    from urllib.parse import urlparse
    host = (urlparse((url or "").strip()).hostname or "").lower()
    return host in ("youtube.com", "youtu.be") or host.endswith((".youtube.com", ".youtu.be"))


def youtube_paused() -> bool:
    return time.time() < _yt_refused_until


def check_youtube(url: str):
    """Raise YouTubeBlocked straight away if YouTube is refusing this server right now."""
    if is_youtube(url) and youtube_paused():
        raise YouTubeBlocked(YT_BLOCKED)


def note_refusal(url: str, err) -> bool:
    """If this error is YouTube refusing the server, pause YouTube links for a while. True if it was."""
    global _yt_refused_until
    if is_youtube(url) and _BOT_CHECK.search(str(err)):
        _yt_refused_until = time.time() + YT_PAUSE
        return True
    return False


@contextlib.contextmanager
def _youtube_guard(url):
    check_youtube(url)
    try:
        yield
    except YouTubeBlocked:
        raise
    except Exception as e:  # noqa: BLE001
        if note_refusal(url, e):
            raise YouTubeBlocked(YT_BLOCKED) from None
        raise


def site_allowed(url: str) -> bool:
    """Links are fetched by the server, so only known video sites are accepted (never arbitrary addresses)."""
    from urllib.parse import urlparse
    try:
        u = urlparse(url.strip())
    except ValueError:
        return False
    host = (u.hostname or "").lower()
    return u.scheme in ("http", "https") and any(host == s or host.endswith("." + s) for s in config.ALLOWED_SITES)


def run(cmd, cwd=None, timeout=3600):
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", creationflags=NO_WINDOW, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"{Path(str(cmd[0])).name} took too long and was stopped.")
    if p.returncode != 0:
        raise RuntimeError(f"{Path(str(cmd[0])).name} failed: {p.stderr[-1500:]}")
    return p.stdout


class _Quiet:
    def debug(self, m): pass
    def info(self, m): pass
    def warning(self, m): pass
    def error(self, m): pass


def _ydl(opts):
    import yt_dlp
    extra = {
        "noplaylist": True, "quiet": True, "no_warnings": True, "logger": _Quiet(),
        "extract_flat": "in_playlist", "playlistend": 1, "socket_timeout": 20,
        "concurrent_fragment_downloads": 4,
        "extractor_args": {
            "youtube": {
                "player_client": ["visionos", "android_vr", "android"],
                "player_skip": ["configs"],
            }
        },
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        },
    }
    proxy = config.env("PEAKCUT_PROXY") or os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
    if proxy:
        extra["proxy"] = proxy
    cookie_file = config.ROOT / "cookies.txt"
    has_cookies = False
    if cookie_file.exists():
        extra["cookiefile"] = str(cookie_file)
        has_cookies = True
    elif config.env("PEAKCUT_YOUTUBE_COOKIES"):
        cpath = config.TMP_DIR / "yt_cookies.txt"
        cpath.write_text(config.env("PEAKCUT_YOUTUBE_COOKIES"), encoding="utf-8")
        extra["cookiefile"] = str(cpath)
        has_cookies = True

    if has_cookies:
        extra["extractor_args"] = {"youtube": {"player_client": ["web", "mweb", "tv", "visionos"]}}
    else:
        extra["extractor_args"] = {
            "youtube": {
                "player_client": ["visionos", "android_vr", "android"],
                "player_skip": ["configs"],
            }
        }
    # a channel or playlist link is answered as "a list" straight away, never walked video by video
    return yt_dlp.YoutubeDL({**extra, **opts})


def _video_format(height):
    return f"bv*[height<={height}]+ba/b[height<={height}]/bv*+ba/b"


def _biggest(job_dir: Path, stem: str) -> Path:
    files = sorted((p for p in job_dir.glob(stem + ".*") if p.suffix not in (".part", ".ytdl")),
                   key=lambda p: p.stat().st_size, reverse=True)
    if not files:
        raise RuntimeError("Download finished but no file was produced.")
    return files[0]


def info(url: str) -> dict:
    """Title, channel and length, without downloading anything."""
    try:
        with _youtube_guard(url), _ydl({}) as ydl:
            i = ydl.extract_info(url, download=False)
    except Exception as e:
        if is_youtube(url):
            fallback_opts = {"extractor_args": {"youtube": {"player_client": ["visionos"]}}}
            with _ydl(fallback_opts) as ydl:
                i = ydl.extract_info(url, download=False)
        else:
            raise e
    return {"title": i.get("title") or "", "uploader": i.get("uploader") or i.get("channel") or "",
            "duration": float(i.get("duration") or 0), "is_live": bool(i.get("is_live")),
            "thumbnail": i.get("thumbnail") or "", "kind": i.get("_type") or "video"}


def youtube_words(url: str):
    """YouTube's own transcript of a video, word by word: ([{'w', 's', 'e'}, ...], language), or None if the video
    has none. Only the transcript in the language that was spoken is used, never a translation."""
    if not is_youtube(url):
        return None
    import json
    try:
        with _youtube_guard(url), _ydl({}) as ydl:
            i = ydl.extract_info(url, download=False)
            auto = i.get("automatic_captions") or {}
            key = next((k for k in auto if k.endswith("-orig")), None)
            if not key and i.get("language") in auto:
                key = i["language"]
            track = next((f for f in auto.get(key) or [] if f.get("ext") == "json3" and f.get("url")), None) if key else None
            if not track:
                return None
            raw = ydl.urlopen(track["url"]).read(40_000_000)
        return _caption_words(json.loads(raw)), key.replace("-orig", "")
    except Exception:
        return None


def youtube_english_words(url: str):
    """YouTube's English captions of a video spoken in another language: ones a person wrote if there are any,
    otherwise YouTube's own translation of its transcript. Word list like youtube_words, or None."""
    if not is_youtube(url):
        return None
    import json
    try:
        with _youtube_guard(url), _ydl({}) as ydl:
            i = ydl.extract_info(url, download=False)
            tracks = []
            for group in (i.get("subtitles") or {}, i.get("automatic_captions") or {}):
                for k in ("en", "en-US", "en-GB"):
                    tracks.append(next((f for f in group.get(k) or [] if f.get("ext") == "json3" and f.get("url")), None))
            track = next((t for t in tracks if t), None)
            if not track:
                return None
            raw = ydl.urlopen(track["url"]).read(40_000_000)
        words = _caption_words(json.loads(raw))
        return words or None
    except Exception:
        return None


def _caption_words(data: dict) -> list:
    out = []
    for ev in data.get("events") or []:
        segs = [x for x in ev.get("segs") or [] if (x.get("utf8") or "").strip()]
        if not segs:
            continue
        t0, dur = float(ev.get("tStartMs") or 0), float(ev.get("dDurationMs") or 0)
        timed = len(segs) > 1 or "tOffsetMs" in segs[0]
        for x in segs:
            parts = x["utf8"].split()
            for n, w in enumerate(parts):
                if re.fullmatch(r"[\[(].*[\])]", w) or w in ("[", "]", "__"):
                    continue                      # [Music], [Applause], and words YouTube blanked out
                # a line with no word timings: its words are spread evenly over the time the line was on screen
                at = t0 + float(x.get("tOffsetMs") or 0) if timed else t0 + dur * n / max(1, len(parts))
                out.append({"w": w, "s": round(at / 1000, 2)})
    out.sort(key=lambda w: w["s"])
    for n, w in enumerate(out):
        nxt = out[n + 1]["s"] if n + 1 < len(out) else w["s"] + 0.4
        w["e"] = round(max(w["s"] + 0.08, min(nxt, w["s"] + 0.6)), 2)
    return out


_PREVIEWS: dict = {}


def preview(url: str) -> dict:
    """What the user will get from a link, checked before any work starts. The thumbnail comes back inline
    (the page may only load images from this site). Cached, so retyping a link costs nothing."""
    url = url.strip()
    if url in _PREVIEWS:
        return _PREVIEWS[url]

    # Fast oEmbed for YouTube: loads in 0.1s without datacenter bot-checks
    if is_youtube(url):
        try:
            import base64
            import urllib.parse
            import urllib.request
            oembed_url = f"https://www.youtube.com/oembed?url={urllib.parse.quote(url)}&format=json"
            req = urllib.request.Request(oembed_url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=4) as r:
                data = json.loads(r.read().decode("utf-8"))
            title = data.get("title") or ""
            author = data.get("author_name") or ""
            thumb_url = data.get("thumbnail_url") or ""
            thumb = ""
            if thumb_url:
                try:
                    treq = urllib.request.Request(thumb_url, headers={"User-Agent": "Mozilla/5.0"})
                    with urllib.request.urlopen(treq, timeout=3) as tr:
                        tdata = tr.read(400_000)
                        thumb = f"data:image/jpeg;base64," + base64.b64encode(tdata).decode("ascii")
                except Exception:
                    thumb = ""
            if title:
                out = {"title": title[:200], "uploader": author[:120], "duration": 0, "is_live": False, "thumb": thumb}
                _PREVIEWS[url] = out
                return out
        except Exception:
            pass

    i = info(url)
    if i["kind"] != "video" or not i["title"]:
        raise ValueError("That link is a channel or playlist, not a single video.")
    thumb = ""
    t = i.get("thumbnail") or ""
    if t.startswith("https://"):
        import base64
        import urllib.request
        try:
            req = urllib.request.Request(t, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=6) as r:
                ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip()
                data = r.read(600_000 + 1)
            if ctype in ("image/jpeg", "image/png", "image/webp") and len(data) <= 600_000:
                thumb = f"data:{ctype};base64," + base64.b64encode(data).decode()
        except Exception:
            thumb = ""
    out = {"title": i["title"][:200], "uploader": i["uploader"][:120], "duration": i["duration"],
           "is_live": i["is_live"], "thumb": thumb}
    if len(_PREVIEWS) > 300:
        _PREVIEWS.pop(next(iter(_PREVIEWS)))
    _PREVIEWS[url] = out
    return out


def save_thumb(job_dir: Path, url: str = "", video: Path = None) -> bool:
    """A small picture for the video list: the link's own thumbnail, or a frame from an uploaded file."""
    import base64
    out = Path(job_dir) / "clips" / "thumb.jpg"
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        if url:
            p = _PREVIEWS.get(url.strip()) or preview(url)
            if not p.get("thumb"):
                return False
            out.write_bytes(base64.b64decode(p["thumb"].split(",", 1)[1]))
            return True
        if video:
            d = probe(video).get("duration") or 0
            run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{min(3.0, d / 3):.2f}", "-i", str(video),
                 "-frames:v", "1", "-vf", "scale=480:-2", "-q:v", "4", str(out)])
            return out.is_file()
    except Exception:
        return False
    return False


def _safe_download(url: str, base_opts: dict):
    try:
        with _youtube_guard(url), _ydl(base_opts) as ydl:
            ydl.download([url])
    except Exception as e:
        if is_youtube(url):
            fallback_opts = {**base_opts, "extractor_args": {"youtube": {"player_client": ["visionos"]}}}
            with _ydl(fallback_opts) as ydl:
                ydl.download([url])
        else:
            raise e


def download_audio(url: str, job_dir: Path, on_progress=None) -> Path:
    """Audio only: a few seconds even for long videos, and all the AI needs to pick clips."""
    def hook(d):
        total = d.get("total_bytes") or d.get("total_bytes_estimate")
        if on_progress and d.get("status") == "downloading" and total:
            on_progress(d.get("downloaded_bytes", 0) / total)

    _safe_download(url, {"format": "ba/b", "outtmpl": str(job_dir / "audio_src.%(ext)s"),
                         "progress_hooks": [hook]})
    return _biggest(job_dir, "audio_src")


def download(url: str, job_dir: Path, height=720) -> Path:
    """The whole video. Used for shorter videos, where this is quicker than fetching pieces."""
    _safe_download(url, {"format": _video_format(height), "merge_output_format": "mp4",
                         "outtmpl": str(job_dir / "source.%(ext)s")})
    return _biggest(job_dir, "source")


def download_section(url: str, out_dir: Path, stem: str, start: float, end: float, height=720, exact=False) -> Path:
    """Just one stretch of the video. The cut lands on a keyframe, so find_offset() pins the true start; with
    exact=True the cut is made at exactly `start` instead (slower), for when there is nothing to match against."""
    from yt_dlp.utils import download_range_func
    out_dir.mkdir(parents=True, exist_ok=True)
    opts = {"format": _video_format(height), "merge_output_format": "mp4", "outtmpl": str(out_dir / (stem + ".%(ext)s")),
            "download_ranges": download_range_func(None, [(max(0.0, start), end)])}
    if exact:
        opts["force_keyframes_at_cuts"] = True
    _safe_download(url, opts)
    return _biggest(out_dir, stem)


def find_offset(full: np.ndarray, part: np.ndarray, expected: float, search=20.0) -> float:
    """Where in the full audio (seconds) a downloaded piece really begins, by matching the sound itself."""
    n = min(len(part), 30 * 16000)
    a0 = max(0, int((expected - search) * 16000))
    hay = full[a0: int((expected + search) * 16000) + n].astype(np.float32)
    needle = part[:n].astype(np.float32)
    if n < 16000 or len(hay) < n or float(np.abs(needle).mean()) < 20:
        return expected  # too short or silent to match: trust the requested time
    size = 1 << (len(hay) + n).bit_length()
    corr = np.fft.irfft(np.fft.rfft(hay, size) * np.conj(np.fft.rfft(needle, size)), size)[: len(hay) - n + 1]
    return (a0 + int(np.argmax(corr))) / 16000


def probe(path: Path) -> dict:
    out = run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)])
    data = json.loads(out)
    v = next((s for s in data["streams"] if s["codec_type"] == "video"), None)
    if not v:
        raise RuntimeError("This file has no video track.")
    if not any(s["codec_type"] == "audio" for s in data["streams"]):
        raise RuntimeError("This file has no audio track, so there is nothing to transcribe.")
    w, h = int(v["width"]), int(v["height"])
    if w * h > 3840 * 2160 or w < 16 or h < 16:
        raise RuntimeError("This video's resolution isn't supported (4K is the largest).")
    rot = str((v.get("side_data_list") or [{}])[0].get("rotation", "0"))
    if rot.lstrip("-") in ("90", "270"):
        w, h = h, w
    return {"width": w, "height": h, "duration": float(data["format"]["duration"])}


def extract_audio(src: Path, wav: Path):
    # first_pts=0 pads any gap before the audio starts, so second 0 of the wav is second 0 of the file
    # -t: never more audio than the longest video allowed, whatever the file claims about itself
    run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000", "-t", str(int(config.MAX_VIDEO_HOURS * 3600) + 60),
         "-af", "aresample=async=1:first_pts=0", "-c:a", "pcm_s16le", str(wav)])


def load_audio(wav: Path) -> np.ndarray:
    data, sr = sf.read(str(wav), dtype="int16")
    assert sr == 16000
    return data


def loudness_per_second(audio: np.ndarray) -> np.ndarray:
    """RMS level in dB for each second of audio."""
    n = len(audio) // 16000
    if n == 0:
        return np.zeros(1, dtype=np.float32)
    x = audio[: n * 16000].reshape(n, 16000).astype(np.float32) / 32768.0
    rms = np.sqrt((x ** 2).mean(axis=1) + 1e-10)
    return (20 * np.log10(rms)).astype(np.float32)
