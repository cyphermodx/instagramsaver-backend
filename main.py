
import os
import uuid
import shutil
import logging
import subprocess
import threading
import time

from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, HttpUrl

import yt_dlp


# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()

BACKEND_URL = "https://instagramsaver-backend.onrender.com"

BASE_DIR = Path("/tmp/media")
BASE_DIR.mkdir(parents=True, exist_ok=True)

# Prevent simultaneous extraction jobs.
PROCESS_LOCK = threading.Lock()

# Pause requests after Instagram rate limiting.
RATE_LIMIT_UNTIL = 0
RATE_LIMIT_SECONDS = 900


# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://instagramsaver.techcypher.workers.dev"
    ],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type"],
)


class RequestData(BaseModel):
    url: HttpUrl


@app.get("/")
def home():
    return {"status": "Instagram Saver backend is running"}


@app.get("/health")
def health():
    return {"status": "ok"}


def is_instagram_url(url):
    host = urlparse(url).hostname or ""

    return host.lower() in (
        "instagram.com",
        "www.instagram.com"
    )


def find_video(folder):
    candidates = [
        f for f in folder.glob("media.*")
        if f.is_file()
        and f.suffix.lower() not in (
            ".mp3", ".part", ".ytdl"
        )
    ]

    return candidates[0] if candidates else None


def extract_audio(video, audio):
    ffmpeg = shutil.which("ffmpeg")

    if not ffmpeg:
        raise RuntimeError("FFmpeg is not installed")

    command = [
        ffmpeg,
        "-y",
        "-i", str(video),
        "-vn",
        "-codec:a", "libmp3lame",
        "-b:a", "128k",
        str(audio)
    ]

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=180
    )

    if result.returncode != 0:
        logger.error("FFmpeg error: %s", result.stderr[-3000:])
        raise RuntimeError("Audio conversion failed")

    if not audio.exists() or audio.stat().st_size == 0:
        raise RuntimeError("MP3 file was not generated")


@app.post("/process")
def process(data: RequestData):

    global RATE_LIMIT_UNTIL

    url = str(data.url)

    if not is_instagram_url(url):
        raise HTTPException(
            status_code=400,
            detail="Please provide a valid Instagram URL."
        )

    with PROCESS_LOCK:

        remaining = RATE_LIMIT_UNTIL - time.monotonic()

        if remaining > 0:
            raise HTTPException(
                status_code=429,
                detail="Instagram is temporarily limiting requests. Please try again later.",
                headers={"Retry-After": str(int(remaining) + 1)}
            )

        if not shutil.which("ffmpeg"):
            raise HTTPException(
                status_code=503,
                detail="Audio conversion service is unavailable."
            )

        job_id = uuid.uuid4().hex
        folder = BASE_DIR / job_id
        folder.mkdir(parents=True, exist_ok=True)

        output = str(folder / "media.%(ext)s")

        options = {
            "outtmpl": output,
            "format": (
                "best[ext=mp4]/"
                "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best"
            ),
            "merge_output_format": "mp4",
            "postprocessors": [{
                "key": "FFmpegVideoRemuxer",
                "preferedformat": "mp4",
            }],
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "socket_timeout": 30,
            "max_filesize": 50 * 1024 * 1024,
        }

        try:
            logger.info("Processing Instagram URL: %s", url)

            # Download Instagram video ONCE.
            with yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(
                    url,
                    download=True
                )

            video = find_video(folder)

            if video is None:
                raise RuntimeError("Video file not found")

            # Convert the already downloaded video locally.
            audio = folder / "audio.mp3"
            extract_audio(video, audio)

            logger.info("Media processing completed: %s", job_id)

            return {
                "title": info.get("title", "Instagram Reel"),
                "author": info.get("uploader", ""),
                "thumbnail": info.get("thumbnail", ""),
                "videoUrl": f"{BACKEND_URL}/download/{job_id}/video",
                "audioUrl": f"{BACKEND_URL}/download/{job_id}/audio",
            }

        except yt_dlp.utils.DownloadError as e:

            error = str(e)

            logger.exception("yt-dlp extraction failed")

            if "429" in error or "Too Many Requests" in error:
                RATE_LIMIT_UNTIL = (
                    time.monotonic() + RATE_LIMIT_SECONDS
                )

                shutil.rmtree(folder, ignore_errors=True)

                raise HTTPException(
                    status_code=429,
                    detail="Instagram is temporarily limiting requests. Please wait before trying again.",
                    headers={"Retry-After": str(RATE_LIMIT_SECONDS)}
                )

            shutil.rmtree(folder, ignore_errors=True)

            raise HTTPException(
                status_code=422,
                detail="Unable to retrieve this Instagram media. It may be unavailable or unsupported."
            )

        except HTTPException:
            shutil.rmtree(folder, ignore_errors=True)
            raise

        except Exception:
            logger.exception("Media processing failed")

            shutil.rmtree(folder, ignore_errors=True)

            raise HTTPException(
                status_code=500,
                detail="Media processing failed. Please try again later."
            )


@app.get("/download/{job_id}/{kind}")
def download(job_id: str, kind: str):

    if kind not in ("video", "audio"):
        raise HTTPException(400, "Invalid download type")

    if (
        len(job_id) != 32
        or not all(c in "0123456789abcdef" for c in job_id)
    ):
        raise HTTPException(400, "Invalid download ID")

    folder = BASE_DIR / job_id

    if not folder.exists():
        raise HTTPException(
            status_code=404,
            detail="Download expired or unavailable"
        )

    if kind == "video":
        file = find_video(folder)

        if file is None:
            raise HTTPException(404, "Video unavailable")

        media_type = (
            "video/mp4"
            if file.suffix.lower() == ".mp4"
            else "application/octet-stream"
        )

        filename = "instagram_video" + file.suffix

    else:
        file = folder / "audio.mp3"

        if not file.exists():
            raise HTTPException(404, "Audio unavailable")

        media_type = "audio/mpeg"
        filename = "instagram_audio.mp3"

    return FileResponse(
        path=file,
        filename=filename,
        media_type=media_type
    )
