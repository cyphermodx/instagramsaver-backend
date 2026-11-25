
import os
import uuid
import shutil
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, HttpUrl
import yt_dlp

app = FastAPI()

# Backend configuration
BACKEND_URL = "https://instagramsaver-backend.onrender.com"

# Allow your frontend website
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://instagramsaver.techcypher.workers.dev"
    ],
    allow_methods=["POST", "GET", "OPTIONS"],
    allow_headers=["Content-Type"],
)

# Temporary media storage
BASE_DIR = Path("/tmp/media")
BASE_DIR.mkdir(parents=True, exist_ok=True)


class RequestData(BaseModel):
    url: HttpUrl


@app.get("/")
def home():
    return {"status": "Instagram Saver backend is running"}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/process")
def process(data: RequestData):

    url = str(data.url)

    if "instagram.com/" not in url:
        raise HTTPException(400, "Invalid Instagram URL")

    job_id = uuid.uuid4().hex
    folder = BASE_DIR / job_id
    folder.mkdir(parents=True)

    output = str(folder / "media.%(ext)s")

    options = {
        "outtmpl": output,
        "format": "best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 20,
        "max_filesize": 50 * 1024 * 1024,
    }

    try:
        # Download video
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=True)

        files = list(folder.glob("media.*"))

        video = next(
            (f for f in files if f.suffix.lower() != ".mp3"),
            None
        )

        if video is None:
            raise RuntimeError("Video file unavailable")

        # Extract audio
        audio_options = {
            "format": "bestaudio/best",
            "outtmpl": str(folder / "audio.%(ext)s"),
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "128",
            }],
        }

        with yt_dlp.YoutubeDL(audio_options) as ydl:
            ydl.extract_info(url, download=True)

        # Return download URLs
        return {
            "title": info.get("title", "Instagram Reel"),
            "author": info.get("uploader", ""),
            "thumbnail": info.get("thumbnail", ""),
            "videoUrl": f"{BACKEND_URL}/download/{job_id}/video",
            "audioUrl": f"{BACKEND_URL}/download/{job_id}/audio",
        }

    except Exception:
        shutil.rmtree(folder, ignore_errors=True)

        raise HTTPException(
            status_code=422,
            detail="Unable to process this media. It may be private, unavailable, or unsupported."
        )


@app.get("/download/{job_id}/{kind}")
def download(job_id: str, kind: str):

    if kind not in ("video", "audio"):
        raise HTTPException(400, "Invalid download type")

    # Prevent invalid folder paths
    if not all(c in "0123456789abcdef" for c in job_id) or len(job_id) != 32:
        raise HTTPException(400, "Invalid download ID")

    folder = BASE_DIR / job_id

    if not folder.exists():
        raise HTTPException(404, "Download expired or unavailable")

    if kind == "video":
        file = next(
            (f for f in folder.glob("media.*")
             if f.suffix.lower() != ".mp3"),
            None
        )
        filename = "instagram_video." + file.suffix.lstrip(".") if file else "instagram_video"
        media_type = "video/mp4" if file and file.suffix.lower() == ".mp4" else "application/octet-stream"

    else:
        file = folder / "audio.mp3"
        filename = "instagram_audio.mp3"
        media_type = "audio/mpeg"

    if file is None or not file.exists():
        raise HTTPException(404, "File unavailable")

    return FileResponse(
        path=file,
        filename=filename,
        media_type=media_type
    )
