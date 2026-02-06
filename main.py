from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from youtube_transcript_api import YouTubeTranscriptApi
import re
import asyncio
from concurrent.futures import ThreadPoolExecutor

app = FastAPI(title="YouTube Transcript Extractor")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_executor = ThreadPoolExecutor(max_workers=5)
_yt_api = YouTubeTranscriptApi()


class TranscriptRequest(BaseModel):
    urls: list[str]
    language: str = "ko"
    denoise: bool = False
    format: str = "text"


def extract_video_id(url: str) -> str | None:
    url = url.strip()
    if not url:
        return None
    patterns = [
        r"(?:youtube\.com/watch\?.*v=)([a-zA-Z0-9_-]{11})",
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


def _fetch_transcript(video_id: str, language: str, denoise: bool, fmt: str) -> dict:
    try:
        languages = [language]
        if language == "ko":
            languages.append("en")
        elif language == "en":
            languages.append("ko")

        data = _yt_api.fetch(video_id, languages=languages)

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
            text = "\n".join(e.text for e in data)
            if denoise:
                text = denoise_text(text)
            return {"transcript": text, "error": None}
    except Exception as e:
        error_msg = str(e)
        if "No transcripts" in error_msg or "Could not retrieve" in error_msg:
            error_msg = "자막을 찾을 수 없습니다."
        elif "disabled" in error_msg.lower():
            error_msg = "이 영상은 자막이 비활성화되어 있습니다."
        elif "unavailable" in error_msg.lower():
            error_msg = "영상을 찾을 수 없습니다."
        return {"transcript": None, "error": error_msg}


@app.post("/api/transcripts")
async def get_transcripts(request: TranscriptRequest):
    if len(request.urls) > 20:
        return JSONResponse(
            status_code=400,
            content={"error": "최대 20개의 URL만 처리할 수 있습니다."},
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
                "transcript": None,
                "error": "유효하지 않은 YouTube URL입니다.",
            }

        result = await loop.run_in_executor(
            _executor,
            _fetch_transcript,
            video_id,
            request.language,
            request.denoise,
            request.format,
        )

        return {
            "url": url,
            "video_id": video_id,
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
