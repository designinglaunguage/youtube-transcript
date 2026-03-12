import logging
import json
import urllib.request
import tempfile
import subprocess
from pathlib import Path

# Load .env file if exists
_env_path = Path(__file__).parent / ".env"
if _env_path.exists():
    for line in _env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            import os as _os
            _os.environ.setdefault(k.strip(), v.strip())

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from youtube_transcript_api import YouTubeTranscriptApi
import re
import time
import asyncio
from concurrent.futures import ThreadPoolExecutor
import os
import urllib.parse
import requests as _requests_mod
import shutil

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="YouTube Transcript Extractor")
# Version: 3.3.0 - Network intercept + ffmpeg direct URL audio extraction

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_executor = ThreadPoolExecutor(max_workers=5)
_fetch_semaphore = asyncio.Semaphore(3)  # max 3 concurrent YouTube fetches

# --- Groq Whisper API (Instagram STT) ---
_groq_api_key = os.environ.get("GROQ_API_KEY", "")
_groq_client = None
if _groq_api_key:
    from groq import Groq
    _groq_client = Groq(api_key=_groq_api_key)
    logger.info("Groq Whisper API initialized")
else:
    logger.info("GROQ_API_KEY not set, Instagram transcription disabled")

_ig_semaphore = asyncio.Semaphore(2)  # max 2 concurrent Instagram transcriptions

# Check if ffmpeg is available for audio extraction
_has_ffmpeg = shutil.which('ffmpeg') is not None
if _has_ffmpeg:
    logger.info("ffmpeg found - will extract audio directly from URL")
else:
    logger.info("ffmpeg not found - will download full video for Groq")

# --- Proxy support (optional PROXY_URL env var) ---
_proxy_url = os.environ.get("PROXY_URL", "")
_proxy_config = None
if _proxy_url:
    from youtube_transcript_api.proxies import GenericProxyConfig
    _proxy_config = GenericProxyConfig(
        http_url=_proxy_url,
        https_url=_proxy_url,
    )
    logger.info(f"Using proxy: {_proxy_url[:30]}...")

# --- Cloudflare Worker proxy support (WORKER_URL env var) ---
_worker_url = os.environ.get("WORKER_URL", "")

class _WorkerProxySession(_requests_mod.Session):
    """Routes requests through a Cloudflare Worker to bypass YouTube IP blocks."""

    def __init__(self, worker_url):
        super().__init__()
        self._worker_url = worker_url.rstrip('/')

    def request(self, method, url, **kwargs):
        if url.startswith('http'):
            proxied = f"{self._worker_url}/?url={urllib.parse.quote(url, safe='')}"
            return super().request(method, proxied, **kwargs)
        return super().request(method, url, **kwargs)

# --- API instances: plain (no cookies) + with cookies (fallback) ---
if _worker_url:
    _worker_session = _WorkerProxySession(_worker_url)
    _yt_api = YouTubeTranscriptApi(http_client=_worker_session)
    logger.info(f"Using Cloudflare Worker proxy: {_worker_url}")
elif _proxy_config:
    _yt_api = YouTubeTranscriptApi(proxy_config=_proxy_config)
else:
    _yt_api = YouTubeTranscriptApi()
_yt_api_cookies = None

_cookie_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookies.txt")

try:
    if not os.path.exists(_cookie_path):
        import base64
        cookies_b64 = os.environ.get("YOUTUBE_COOKIES_BASE64", "")
        if cookies_b64:
            _tmp_cookie = "/tmp/cookies.txt"
            with open(_tmp_cookie, "wb") as f:
                f.write(base64.b64decode(cookies_b64))
            _cookie_path = _tmp_cookie
            logger.info("Created cookies.txt from YOUTUBE_COOKIES_BASE64 env var")

    if os.path.exists(_cookie_path):
        import http.cookiejar
        import requests
        _cookie_jar = http.cookiejar.MozillaCookieJar(_cookie_path)
        _cookie_jar.load(ignore_discard=True, ignore_expires=True)
        if _worker_url:
            _session = _WorkerProxySession(_worker_url)
        else:
            _session = requests.Session()
        _session.cookies = _cookie_jar
        if _proxy_url and not _worker_url:
            _session.proxies = {"http": _proxy_url, "https": _proxy_url}
        _yt_api_cookies = YouTubeTranscriptApi(http_client=_session)
        logger.info(f"Cookies loaded from {_cookie_path} (used as fallback, worker={'yes' if _worker_url else 'no'})")
    else:
        logger.info("No cookies found, running without cookies")
except Exception as e:
    logger.error(f"Failed to load cookies: {e}")


class TranscriptRequest(BaseModel):
    urls: list[str]
    language: str = "auto"
    denoise: bool = False
    format: str = "text"  # text, json, srt, vtt
    keep_newlines: bool = False
    timestamps: bool = False


class PlaylistRequest(BaseModel):
    url: str


class FeedbackRequest(BaseModel):
    message: str
    type: str = "general"


def extract_video_id(url: str) -> str | None:
    url = url.strip()
    if not url:
        return None
    url = re.sub(r'[&?](si|feature|utm_\w+|fbclid|gclid)=[^&]*', '', url)
    patterns = [
        r"(?:(?:m\.)?youtube\.com/watch\?.*v=)([a-zA-Z0-9_-]{11})",
        r"(?:youtu\.be/)([a-zA-Z0-9_-]{11})",
        r"(?:youtube\.com/embed/)([a-zA-Z0-9_-]{11})",
        r"(?:youtube\.com/shorts/)([a-zA-Z0-9_-]{11})",
        r"^([a-zA-Z0-9_-]{11})$",
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None


def detect_platform(url: str) -> tuple[str, str | None]:
    """Returns (platform, content_id) tuple."""
    url = url.strip()
    if not url:
        return ("unknown", None)
    ig_match = re.search(
        r"(?:instagram\.com|instagr\.am)/(?:reel|reels|p|tv)/([A-Za-z0-9_-]+)", url
    )
    if ig_match:
        return ("instagram", ig_match.group(1))
    yt_id = extract_video_id(url)
    if yt_id:
        return ("youtube", yt_id)
    return ("unknown", None)


def _fetch_title(video_id: str) -> str | None:
    try:
        oembed_url = f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={video_id}&format=json"
        req = urllib.request.Request(oembed_url)
        with urllib.request.urlopen(req, timeout=5) as response:
            data = json.loads(response.read().decode("utf-8"))
            return data.get("title")
    except Exception:
        return None


KOREAN_FILLERS = {
    "어", "음", "그", "아", "네", "예", "에", "으", "흠",
    "어어", "음음", "아아", "네네", "예예",
}

NOISE_PATTERN = re.compile(r"^\[.*\]$")


def denoise_text(text: str) -> str:
    lines = text.split("\n")
    result = []
    prev = None
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped in KOREAN_FILLERS:
            continue
        if NOISE_PATTERN.match(stripped):
            continue
        if stripped == prev:
            continue
        result.append(stripped)
        prev = stripped
    return "\n".join(result)


def _format_ts_short(seconds: float) -> str:
    m = int(seconds // 60)
    s = int(seconds % 60)
    return f"{m}:{s:02d}"


def _format_ts_srt(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds % 1) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _format_ts_vtt(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds % 1) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def _format_srt(entries: list[dict]) -> str:
    lines = []
    for i, e in enumerate(entries, 1):
        start = _format_ts_srt(e["start"])
        end = _format_ts_srt(e["start"] + e["duration"])
        lines.append(str(i))
        lines.append(f"{start} --> {end}")
        lines.append(e["text"])
        lines.append("")
    return "\n".join(lines)


def _format_vtt(entries: list[dict]) -> str:
    lines = ["WEBVTT", ""]
    for e in entries:
        start = _format_ts_vtt(e["start"])
        end = _format_ts_vtt(e["start"] + e["duration"])
        lines.append(f"{start} --> {end}")
        lines.append(e["text"])
        lines.append("")
    return "\n".join(lines)


def _format_error(error_msg: str) -> str:
    if "No transcripts" in error_msg or "Could not retrieve" in error_msg:
        return f"No subtitles found. ({error_msg[:200]})"
    elif "disabled" in error_msg.lower():
        return "Subtitles are disabled for this video."
    elif "unavailable" in error_msg.lower():
        return "Video not found."
    return error_msg


def _fetch_transcript(video_id: str, language: str, denoise: bool, fmt: str, keep_newlines: bool = False, timestamps: bool = False) -> dict:
    if language == "auto":
        languages = ["en", "ko", "ja", "es", "pt"]
    else:
        languages = [language]

    apis_to_try = [("plain", _yt_api)]
    if _yt_api_cookies:
        apis_to_try.append(("cookies", _yt_api_cookies))

    def _process_result(data):
        entries = [
            {"text": e.text, "start": e.start, "duration": e.duration}
            for e in data
        ]
        if denoise:
            deduped = []
            prev_text = None
            for entry in entries:
                txt = entry["text"].strip()
                if txt in KOREAN_FILLERS or NOISE_PATTERN.match(txt):
                    continue
                if txt == prev_text:
                    continue
                if txt:
                    entry["text"] = txt
                    deduped.append(entry)
                    prev_text = txt
            entries = deduped

        if fmt == "json":
            return {"transcript": entries, "error": None}
        elif fmt == "srt":
            return {"transcript": _format_srt(entries), "error": None}
        elif fmt == "vtt":
            return {"transcript": _format_vtt(entries), "error": None}
        else:  # text
            if timestamps:
                lines = []
                for e in entries:
                    ts = _format_ts_short(e["start"])
                    lines.append("[" + ts + "] " + e["text"])
                text = "\n".join(lines)
            else:
                separator = "\n" if keep_newlines else " "
                text = separator.join(e["text"] for e in entries)
                if not keep_newlines:
                    text = " ".join(text.split())
            return {"transcript": text, "error": None}

    max_retries = 4
    for attempt in range(max_retries):
        last_error = None
        for api_name, api in apis_to_try:
            try:
                data = api.fetch(video_id, languages=languages)
                return _process_result(data)
            except Exception as e:
                last_error = str(e)
                logger.warning(f"[{api_name}] attempt {attempt+1} Failed for {video_id}: {last_error[:200]}")

                if "No transcripts" in last_error or "disabled" in last_error.lower():
                    return {"transcript": None, "error": _format_error(last_error)}

        if attempt < max_retries - 1:
            delay = 2 ** (attempt + 1)
            logger.info(f"Retrying {video_id} after {delay}s delay (attempt {attempt+1})")
            time.sleep(delay)

    for api_name, api in apis_to_try:
        try:
            logger.info(f"[{api_name}] Trying without language filter for {video_id}")
            data = api.fetch(video_id)
            return _process_result(data)
        except Exception as e:
            logger.warning(f"[{api_name}] No-lang fallback failed for {video_id}: {str(e)[:200]}")

    for api_name, api in apis_to_try:
        try:
            logger.info(f"[{api_name}] Listing transcripts for {video_id}")
            transcript_list = api.list(video_id)
            for lang in languages:
                for t in transcript_list:
                    if t.language_code == lang:
                        data = t.fetch()
                        return _process_result(data)
            for t in transcript_list:
                data = t.fetch()
                return _process_result(data)
        except Exception as e:
            logger.warning(f"[{api_name}] List fallback failed for {video_id}: {str(e)[:200]}")

    return {"transcript": None, "error": _format_error(last_error or "Unknown error")}


# ---------------------------------------------------------------------------
# Instagram video URL extraction: 2-tier cascade
#   1. Playwright embed page (cookie-free) + network intercept
#   2. Playwright full page with cookies (fallback for private/restricted)
#
# Optimizations:
#   - Dedicated single-thread executor for Playwright (thread-safety)
#   - Persistent browser instance pre-warmed at startup
#   - Network intercept captures CDN URL before DOM renders (fastest)
#   - ffmpeg extracts audio directly from URL (skip full video download)
# ---------------------------------------------------------------------------

_pw_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='playwright')
_ig_browser = None
_ig_pw = None


def _pw_init_browser():
    """Initialize persistent browser. Must run inside _pw_executor thread."""
    global _ig_browser, _ig_pw
    if _ig_browser and _ig_browser.is_connected():
        return _ig_browser
    if _ig_pw:
        try:
            _ig_pw.stop()
        except Exception:
            pass
    from playwright.sync_api import sync_playwright
    _ig_pw = sync_playwright().start()
    _ig_browser = _ig_pw.chromium.launch(
        headless=True,
        args=['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu'],
    )
    logger.info("[instagram] Launched persistent Chromium browser")
    return _ig_browser


# Pre-warm browser at import time (fire-and-forget; failure is non-fatal)
try:
    _pw_executor.submit(_pw_init_browser)
except Exception:
    logger.warning("[instagram] Failed to submit browser pre-warm task")


def _pw_extract_embed(shortcode):
    """Run inside _pw_executor thread. Extract video URL from embed page via DOM."""
    browser = _pw_init_browser()
    ctx = browser.new_context(
        user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
        viewport={'width': 1280, 'height': 720},
    )
    page = ctx.new_page()
    page.goto(
        f'https://www.instagram.com/p/{shortcode}/embed/',
        wait_until='domcontentloaded',
        timeout=15000,
    )

    # Wait for <video src=...> element (typically appears in ~1s with warm browser)
    video_url = None
    try:
        video_el = page.wait_for_selector('video[src]', timeout=5000)
        if video_el:
            src = video_el.get_attribute('src')
            if src and src.startswith('http'):
                video_url = src
    except Exception:
        video_el = page.query_selector('video')
        if video_el:
            src = video_el.get_attribute('src')
            if src and src.startswith('http'):
                video_url = src

    title = None
    caption_el = page.query_selector('.Caption, .CaptionUsername')
    if caption_el:
        title = caption_el.inner_text()[:100]
    if not title:
        og_title = page.query_selector('meta[property="og:title"]')
        if og_title:
            title = og_title.get_attribute('content')

    ctx.close()
    return video_url, title


def _pw_extract_with_cookies(url, pw_cookies):
    """Run inside _pw_executor thread. Extract video URL using cookies + GraphQL intercept."""
    browser = _pw_init_browser()
    ctx = browser.new_context(
        user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
        viewport={'width': 1280, 'height': 720},
    )
    ctx.add_cookies(pw_cookies)
    page = ctx.new_page()

    video_urls = []
    titles = []

    def _dig_video(obj, vlist, tlist, depth=0):
        if depth > 20:
            return
        if isinstance(obj, dict):
            vu = obj.get('video_url')
            if vu and isinstance(vu, str) and vu.startswith('http'):
                vlist.append(vu)
            vv = obj.get('video_versions')
            if isinstance(vv, list):
                for v in vv:
                    if isinstance(v, dict) and v.get('url'):
                        vlist.append(v['url'])
            cap = obj.get('caption')
            if isinstance(cap, dict) and cap.get('text'):
                tlist.append(cap['text'][:100])
            cap_edges = obj.get('edge_media_to_caption')
            if isinstance(cap_edges, dict):
                edges = cap_edges.get('edges', [])
                if edges and isinstance(edges[0], dict):
                    node = edges[0].get('node', {})
                    if isinstance(node, dict) and node.get('text'):
                        tlist.append(node['text'][:100])
            for v in obj.values():
                _dig_video(v, vlist, tlist, depth + 1)
        elif isinstance(obj, list):
            for item in obj:
                _dig_video(item, vlist, tlist, depth + 1)

    def _on_resp(resp):
        if resp.status != 200:
            return
        u = resp.url
        if 'graphql' not in u and '/api/v1/' not in u:
            return
        ct = resp.headers.get('content-type', '')
        if 'json' not in ct and 'text' not in ct:
            return
        try:
            body = resp.text()
            if 'video_url' in body or 'video_versions' in body:
                _dig_video(json.loads(body), video_urls, titles)
        except Exception:
            pass

    page.on('response', _on_resp)
    page.goto(url, wait_until='domcontentloaded', timeout=15000)
    for _ in range(10):
        page.wait_for_timeout(500)
        if video_urls:
            break

    page_title = page.evaluate("""() => {
        const d = document.querySelector('meta[property="og:description"]');
        if (d) return d.content;
        const t = document.querySelector('meta[property="og:title"]');
        if (t) return t.content;
        return document.title || null;
    }""")
    ctx.close()

    title = titles[0] if titles else page_title
    return video_urls[0] if video_urls else None, title


def _extract_ig_video_url_embed(shortcode):
    """Extract video URL from embed page. Dispatches to dedicated Playwright thread."""
    try:
        future = _pw_executor.submit(_pw_extract_embed, shortcode)
        video_url, title = future.result(timeout=25)
        if video_url:
            logger.info(f"[embed/playwright] Extracted video URL for {shortcode}")
            return video_url, title, None
        return None, title, "No video element found in embed page"
    except Exception as e:
        return None, None, f"Embed extraction failed: {str(e)[:200]}"


def _extract_ig_video_url(url):
    """Extract Instagram video URL. Tries cookie-free embed first, falls back to authenticated Playwright."""
    ig_match = re.search(
        r"(?:instagram\.com|instagr\.am)/(?:reel|reels|p|tv)/([A-Za-z0-9_-]+)", url
    )
    shortcode = ig_match.group(1) if ig_match else None

    if shortcode:
        logger.info(f"[instagram] Trying embed page (no cookies) for {shortcode}")
        video_url, title, err = _extract_ig_video_url_embed(shortcode)
        if video_url:
            return video_url, title, None
        logger.info(f"[instagram] Embed failed: {err}")

    logger.info(f"[instagram] Falling back to Playwright with cookies for {url}")
    return _extract_ig_video_url_playwright(url)


def _extract_ig_video_url_playwright(url):
    """Use Playwright with cookies to extract video URL. Dispatches to dedicated Playwright thread."""
    import http.cookiejar as _hcj

    _ig_cookie_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "instagram_cookies.txt")
    if not os.path.exists(_ig_cookie_path):
        import base64
        ig_cookies_b64 = os.environ.get("INSTAGRAM_COOKIES_BASE64", "")
        if ig_cookies_b64:
            _ig_cookie_path = os.path.join(tempfile.gettempdir(), "instagram_cookies.txt")
            with open(_ig_cookie_path, "wb") as f:
                f.write(base64.b64decode(ig_cookies_b64))
            logger.info("Created instagram_cookies.txt from INSTAGRAM_COOKIES_BASE64 env var")
    pw_cookies = []
    if os.path.exists(_ig_cookie_path):
        cj = _hcj.MozillaCookieJar(_ig_cookie_path)
        cj.load(ignore_discard=True, ignore_expires=True)
        for c in cj:
            cookie = {'name': c.name, 'value': c.value, 'domain': c.domain, 'path': c.path}
            if c.expires:
                cookie['expires'] = c.expires
            if c.secure:
                cookie['secure'] = True
            pw_cookies.append(cookie)

    if not pw_cookies:
        return None, None, "Instagram cookies not found. Please provide instagram_cookies.txt."

    try:
        future = _pw_executor.submit(_pw_extract_with_cookies, url, pw_cookies)
        video_url, title = future.result(timeout=25)
        if video_url:
            return video_url, title, None
        return None, title, "Could not extract video URL. The video may be private or unavailable."
    except Exception as e:
        return None, None, f"Browser extraction failed: {str(e)[:200]}"


def _download_audio(video_url, tmpdir):
    """Download video and prepare audio file for Groq.

    If ffmpeg available: extract audio directly from URL (5MB video -> ~300KB audio).
    Otherwise: download full video file.
    """
    if _has_ffmpeg:
        audio_path = os.path.join(tmpdir, 'audio.m4a')
        try:
            subprocess.run(
                ['ffmpeg', '-i', video_url,
                 '-headers', 'User-Agent: Mozilla/5.0\r\nReferer: https://www.instagram.com/\r\n',
                 '-vn', '-acodec', 'aac', '-b:a', '64k',
                 '-y', '-loglevel', 'error', audio_path],
                timeout=20, check=True, capture_output=True,
            )
            size = os.path.getsize(audio_path) if os.path.exists(audio_path) else 0
            if size > 100:
                logger.info(f"[instagram] Audio extracted from URL: {size/1024:.0f}KB")
                return audio_path, 'audio.m4a'
        except Exception as e:
            logger.warning(f"[instagram] ffmpeg URL extraction failed: {e}")

    # Fallback: download full video
    video_path = os.path.join(tmpdir, 'video.mp4')
    r = _requests_mod.get(video_url, headers={
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Referer': 'https://www.instagram.com/',
    }, timeout=30, stream=True)
    with open(video_path, 'wb') as f:
        for chunk in r.iter_content(chunk_size=65536):
            f.write(chunk)
    size = os.path.getsize(video_path)
    if size < 1024:
        raise ValueError("Downloaded video is too small")
    logger.info(f"[instagram] Downloaded full video: {size/1024:.0f}KB")
    return video_path, 'video.mp4'


def _fetch_instagram_transcript(url, language, denoise_flag, fmt, keep_newlines=False, timestamps=False):
    if not _groq_client:
        return {"transcript": None, "error": "Instagram transcription not configured (GROQ_API_KEY missing).", "title": None}

    t0 = time.time()

    # Step 1: Extract video URL
    video_url, title, err = _extract_ig_video_url(url)
    t1 = time.time()
    logger.info(f"[instagram] Step1 URL extraction: {t1-t0:.1f}s")
    if err:
        return {"transcript": None, "error": err, "title": title}

    with tempfile.TemporaryDirectory() as tmpdir:
        # Step 2: Download/extract audio
        try:
            upload_path, filename = _download_audio(video_url, tmpdir)
        except Exception as e:
            return {"transcript": None, "error": f"Audio download failed: {str(e)[:200]}", "title": title}

        t2 = time.time()
        logger.info(f"[instagram] Step2 download/audio: {t2-t1:.1f}s")

        # Step 3: Transcribe with Groq Whisper API
        try:
            with open(upload_path, "rb") as audio_file:
                result = _groq_client.audio.transcriptions.create(
                    file=(filename, audio_file),
                    model="whisper-large-v3-turbo",
                    response_format="verbose_json",
                    language=None if language == "auto" else language,
                    temperature=0.0,
                )
        except Exception as e:
            return {"transcript": None, "error": f"Transcription failed: {str(e)[:200]}", "title": title}

    t3 = time.time()
    logger.info(f"[instagram] Step3 Groq STT: {t3-t2:.1f}s")
    logger.info(f"[instagram] TOTAL: {t3-t0:.1f}s")

    # Step 4: Build entries from segments
    entries = []
    if hasattr(result, 'segments') and result.segments:
        for seg in result.segments:
            entries.append({
                "text": seg.get("text", "").strip() if isinstance(seg, dict) else seg.text.strip(),
                "start": seg.get("start", 0) if isinstance(seg, dict) else seg.start,
                "duration": (seg.get("end", 0) - seg.get("start", 0)) if isinstance(seg, dict) else (seg.end - seg.start),
            })
    elif hasattr(result, 'text') and result.text:
        entries = [{"text": result.text.strip(), "start": 0, "duration": 0}]

    if not entries:
        return {"transcript": "", "error": None, "title": title}

    if denoise_flag:
        deduped = []
        prev_text = None
        for entry in entries:
            txt = entry["text"].strip()
            if txt in KOREAN_FILLERS or NOISE_PATTERN.match(txt):
                continue
            if txt == prev_text:
                continue
            if txt:
                entry["text"] = txt
                deduped.append(entry)
                prev_text = txt
        entries = deduped

    if fmt == "json":
        return {"transcript": entries, "error": None, "title": title}
    elif fmt == "srt":
        return {"transcript": _format_srt(entries), "error": None, "title": title}
    elif fmt == "vtt":
        return {"transcript": _format_vtt(entries), "error": None, "title": title}
    else:  # text
        if timestamps:
            lines = ["[" + _format_ts_short(e["start"]) + "] " + e["text"] for e in entries]
            text = "\n".join(lines)
        else:
            separator = "\n" if keep_newlines else " "
            text = separator.join(e["text"] for e in entries)
            if not keep_newlines:
                text = " ".join(text.split())
        return {"transcript": text, "error": None, "title": title}


@app.post("/api/transcripts")
async def get_transcripts(request: TranscriptRequest):
    if len(request.urls) > 100:
        return JSONResponse(
            status_code=400,
            content={"error": "Maximum 100 URLs allowed."},
        )

    urls = [u.strip() for u in request.urls if u.strip()]
    if not urls:
        return JSONResponse(
            status_code=400,
            content={"error": "Please enter at least one URL."},
        )

    loop = asyncio.get_event_loop()

    async def process_url(url: str):
        platform, content_id = detect_platform(url)

        if platform == "unknown" or content_id is None:
            return {
                "url": url,
                "video_id": None,
                "platform": "unknown",
                "title": None,
                "transcript": None,
                "error": "Invalid URL. YouTube and Instagram URLs are supported.",
            }

        if platform == "instagram":
            async with _ig_semaphore:
                result = await loop.run_in_executor(
                    _executor, _fetch_instagram_transcript,
                    url, request.language, request.denoise,
                    request.format, request.keep_newlines, request.timestamps,
                )
            return {
                "url": url,
                "video_id": content_id,
                "platform": "instagram",
                "title": result.get("title"),
                "transcript": result["transcript"],
                "error": result["error"],
            }

        # YouTube
        async with _fetch_semaphore:
            result, title = await asyncio.gather(
                loop.run_in_executor(
                    _executor,
                    _fetch_transcript,
                    content_id,
                    request.language,
                    request.denoise,
                    request.format,
                    request.keep_newlines,
                    request.timestamps,
                ),
                loop.run_in_executor(_executor, _fetch_title, content_id),
            )

        return {
            "url": url,
            "video_id": content_id,
            "platform": "youtube",
            "title": title,
            "transcript": result["transcript"],
            "error": result["error"],
        }

    results = list(await asyncio.gather(*[process_url(url) for url in urls]))

    success_count = sum(1 for r in results if r["error"] is None)
    error_count = sum(1 for r in results if r["error"] is not None)

    return {
        "results": results,
        "total": len(urls),
        "success_count": success_count,
        "error_count": error_count,
    }


def _resolve_playlist(url: str) -> list[str]:
    """Extract video IDs from a YouTube playlist URL."""
    match = re.search(r'[?&]list=([a-zA-Z0-9_-]+)', url)
    if not match:
        return []
    playlist_id = match.group(1)
    try:
        playlist_url = f"https://www.youtube.com/playlist?list={playlist_id}"
        req = urllib.request.Request(playlist_url, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })
        with urllib.request.urlopen(req, timeout=15) as response:
            html = response.read().decode('utf-8')
        video_ids = list(dict.fromkeys(re.findall(r'"videoId":"([a-zA-Z0-9_-]{11})"', html)))
        return video_ids
    except Exception as e:
        logger.warning(f"Failed to resolve playlist {playlist_id}: {e}")
        return []


@app.post("/api/playlist")
async def resolve_playlist(request: PlaylistRequest):
    loop = asyncio.get_event_loop()
    video_ids = await loop.run_in_executor(_executor, _resolve_playlist, request.url)
    if not video_ids:
        return JSONResponse(
            status_code=400,
            content={"error": "Could not resolve playlist. It may be private or empty."},
        )
    return {
        "video_ids": video_ids,
        "urls": [f"https://www.youtube.com/watch?v={vid}" for vid in video_ids],
        "count": len(video_ids),
    }


@app.post("/api/feedback")
async def submit_feedback(request: FeedbackRequest):
    if not request.message.strip():
        return JSONResponse(status_code=400, content={"error": "Empty feedback"})
    if len(request.message) > 2000:
        return JSONResponse(status_code=400, content={"error": "Feedback too long (max 2000 chars)"})

    feedback_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "feedback.json")
    feedbacks = []
    if os.path.exists(feedback_path):
        try:
            with open(feedback_path, "r", encoding="utf-8") as f:
                feedbacks = json.load(f)
        except Exception:
            feedbacks = []

    from datetime import datetime, timezone
    feedbacks.append({
        "message": request.message.strip(),
        "type": request.type,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })

    with open(feedback_path, "w", encoding="utf-8") as f:
        json.dump(feedbacks, f, ensure_ascii=False, indent=2)

    return {"success": True}


@app.get("/")
async def root():
    return FileResponse("static/index.html")


@app.get("/health")
async def health_check():
    """Lightweight health check for Railway. No external dependencies."""
    return {"status": "ok"}
