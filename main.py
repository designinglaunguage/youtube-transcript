import logging
import json
import urllib.request
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

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="YouTube Transcript Extractor")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_executor = ThreadPoolExecutor(max_workers=5)

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
        _session = requests.Session()
        _session.cookies = _cookie_jar
        if _proxy_url:
            _session.proxies = {"http": _proxy_url, "https": _proxy_url}
        _yt_api_cookies = YouTubeTranscriptApi(http_client=_session)
        logger.info(f"Cookies loaded from {_cookie_path} (used as fallback)")
    else:
        logger.info("No cookies found, running without cookies")
except Exception as e:
    logger.error(f"Failed to load cookies: {e}")


class TranscriptRequest(BaseModel):
    urls: list[str]
    language: str = "ko"
    denoise: bool = False
    format: str = "text"
    keep_newlines: bool = False


def extract_video_id(url: str) -> str | None:
    url = url.strip()
    if not url:
        return None
    # Remove tracking parameters
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


def _fetch_transcript(video_id: str, language: str, denoise: bool, fmt: str, keep_newlines: bool = False) -> dict:
    languages = [language]
    if language == "ko":
        languages.append("en")
    elif language == "en":
        languages.append("ko")

    apis_to_try = [("plain", _yt_api)]
    if _yt_api_cookies:
        apis_to_try.append(("cookies", _yt_api_cookies))

    last_error = None
    for api_name, api in apis_to_try:
        try:
            data = api.fetch(video_id, languages=languages)

            if fmt == "json":
                entries = [
                    {"text": e.text, "start": e.start, "duration": e.duration}
                    for e in data
                ]
                if denoise:
                    deduped = []
                    prev_text = None
                    for entry in entries:
                        t = entry["text"].strip()
                        if t in KOREAN_FILLERS or NOISE_PATTERN.match(t):
                            continue
                        if t == prev_text:
                            continue
                        if t:
                            entry["text"] = t
                            deduped.append(entry)
                            prev_text = t
                    entries = deduped
                return {"transcript": entries, "error": None}
            else:
                separator = "\n" if keep_newlines else " "
                text = separator.join(e.text for e in data)
                if denoise:
                    text = denoise_text(text)
                if not keep_newlines:
                    text = " ".join(text.split())
                return {"transcript": text, "error": None}
        except Exception as e:
            last_error = str(e)
            logger.warning(f"[{api_name}] Failed for {video_id}: {last_error[:100]}")

            # Don't try cookies fallback if video genuinely has no subtitles
            if "No transcripts" in last_error or "disabled" in last_error.lower():
                break

    # All attempts failed
    error_msg = last_error or "Unknown error"
    if "No transcripts" in error_msg or "Could not retrieve" in error_msg:
        error_msg = f"자막을 찾을 수 없습니다. ({error_msg[:120]})"
    elif "disabled" in error_msg.lower():
        error_msg = "이 영상은 자막이 비활성화되어 있습니다."
    elif "unavailable" in error_msg.lower():
        error_msg = "영상을 찾을 수 없습니다."
    return {"transcript": None, "error": error_msg}


@app.post("/api/transcripts")
async def get_transcripts(request: TranscriptRequest):
    if len(request.urls) > 50:
        return JSONResponse(
            status_code=400,
            content={"error": "최대 50개의 URL만 처리할 수 있습니다."},
        )

    urls = [u.strip() for u in request.urls if u.strip()]
    if not urls:
        return JSONResponse(
            status_code=400,
            content={"error": "URL을 하나 이상 입력해주세요."},
        )

    loop = asyncio.get_event_loop()

    async def process_url(url: str):
        video_id = extract_video_id(url)
        if not video_id:
            return {
                "url": url,
                "video_id": None,
                "title": None,
                "transcript": None,
                "error": "유효하지 않은 YouTube URL입니다.",
            }

        result, title = await asyncio.gather(
            loop.run_in_executor(
                _executor,
                _fetch_transcript,
                video_id,
                request.language,
                request.denoise,
                request.format,
                request.keep_newlines,
            ),
            loop.run_in_executor(_executor, _fetch_title, video_id),
        )

        return {
            "url": url,
            "video_id": video_id,
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


@app.get("/")
async def root():
    return FileResponse("static/index.html")
