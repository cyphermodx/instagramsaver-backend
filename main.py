
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


# --------------------------------------------------
# CONFIGURATION
# --------------------------------------------------

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("instagram_saver")

app = FastAPI()

BACKEND_URL = "https://instagramsaver-backend.onrender.com"

BASE_DIR = Path("/tmp/media")
BASE_DIR.mkdir(parents=True, exist_ok=True)

MAX_FILE_SIZE = 50 * 1024 * 1024

# Temporary protection (single Python process only).
PROCESS_LOCK = threading.Lock()
STATE_LOCK = threading.Lock()

RATE_LIMIT_UNTIL = 0
RATE_LIMIT_SECONDS = 900

# Completed results are cached in memory for 20 minutes.
CACHE_TTL = 1200
RESULT_CACHE = {}


# --------------------------------------------------
# CORS
# --------------------------------------------------

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://instagramsaver.techcypher.workers.dev"
    ],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type"],
)


# --------------------------------------------------
# REQUEST MODEL
# --------------------------------------------------

class RequestData(BaseModel):
    url: HttpUrl


# --------------------------------------------------
# BASIC ENDPOINTS
# --------------------------------------------------

@app.get("/")
def home():
    return {
        "status": "Instagram Saver backend is running"
    }


@app.get("/health")
def health():
    return {
        "status": "ok"
    }


# --------------------------------------------------
# URL VALIDATION
# --------------------------------------------------

def validate_instagram_url(url):

    host = (urlparse(url).hostname or "").lower()

    allowed_hosts = {
        "instagram.com",
        "www.instagram.com",
    }

    if host not in allowed_hosts:
        raise HTTPException(
            status_code=400,
            detail="Please enter a valid Instagram URL."
        )

    return True


def normalize_url(url):

    parsed = urlparse(url)

    return (
        parsed.scheme.lower()
        + "://"
        + parsed.netloc.lower()
        + parsed.path.rstrip("/")
    )


# --------------------------------------------------
# RATE LIMIT PROTECTION
# --------------------------------------------------

def check_cooldown():

    remaining = RATE_LIMIT_UNTIL - time.monotonic()

    if remaining > 0:

        raise HTTPException(
            status_code=429,
            detail=(
                "Instagram is temporarily limiting requests. "
                "Please try again later."
            ),
            headers={
                "Retry-After": str(int(remaining) + 1)
            }
        )


def activate_cooldown():

    global RATE_LIMIT_UNTIL

    with STATE_LOCK:

        RATE_LIMIT_UNTIL = (
            time.monotonic() + RATE_LIMIT_SECONDS
        )

    logger.warning(
        "Instagram rate limit detected. "
        "Extraction paused for %s seconds.",
        RATE_LIMIT_SECONDS
    )


def is_rate_limit_error(error):

    message = str(error).lower()

    return (
        "429" in message
        or "too many requests" in message
    )


# --------------------------------------------------
# FILE HELPERS
# --------------------------------------------------

def find_video(folder):

    excluded = {
        ".mp3",
        ".part",
        ".ytdl",
    }

    candidates = [
        file for file in folder.glob("media.*")
        if file.is_file()
        and file.suffix.lower() not in excluded
    ]

    return candidates[0] if candidates else None


def create_mp3(video, audio):

    ffmpeg = shutil.which("ffmpeg")

    if not ffmpeg:
        raise RuntimeError(
            "FFmpeg is not installed on the server."
        )

    command = [
        ffmpeg,
        "-y",
        "-i", str(video),
        "-vn",
        "-codec:a", "libmp3lame",
        "-b:a", "128k",
        str(audio),
    ]

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=180,
    )

    if result.returncode != 0:

        logger.error(
            "FFmpeg conversion failed: %s",
            result.stderr[-2000:]
        )

        raise RuntimeError("MP3 conversion failed.")

    if not audio.exists() or audio.stat().st_size == 0:

        raise RuntimeError("MP3 file was not generated.")


# --------------------------------------------------
# DUPLICATE RESULT CACHE
# --------------------------------------------------

def get_cached_result(key):

    with STATE_LOCK:

        item = RESULT_CACHE.get(key)

        if not item:
            return None

        if time.monotonic() - item["time"] > CACHE_TTL:

            RESULT_CACHE.pop(key, None)
            return None

        folder = BASE_DIR / item["job_id"]

        if not folder.exists():
            RESULT_CACHE.pop(key, None)
            return None

        return item["response"]


def save_cached_result(key, job_id, response):

    with STATE_LOCK:

        RESULT_CACHE[key] = {
            "job_id": job_id,
            "response": response,
            "time": time.monotonic(),
        }


# --------------------------------------------------
# PROCESS INSTAGRAM MEDIA
# --------------------------------------------------

@app.post("/process")
def process(data: RequestData):

    url = str(data.url)

    validate_instagram_url(url)

    cache_key = normalize_url(url)

    # Reuse recently completed results.
    cached = get_cached_result(cache_key)

    if cached:
        logger.info("Returning cached media result.")
        return cached

    # Only one extraction at a time in this process.
    if not PROCESS_LOCK.acquire(blocking=False):

        raise HTTPException(
            status_code=503,
            detail=(
                "The service is currently processing another request. "
                "Please try again shortly."
            ),
            headers={"Retry-After": "15"}
        )

    job_id = None
    folder = None

    try:

        # Check again after acquiring the lock.
        cached = get_cached_result(cache_key)

        if cached:
            return cached

        check_cooldown()

        if not shutil.which("ffmpeg"):

            raise HTTPException(
                status_code=503,
                detail="Audio conversion is temporarily unavailable."
            )

        job_id = uuid.uuid4().hex
        folder = BASE_DIR / job_id

        folder.mkdir(parents=True, exist_ok=True)

        output = str(folder / "media.%(ext)s")

        options = {
            "outtmpl": output,
            "format": "best[ext=mp4]/best",
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "socket_timeout": 30,
            "max_filesize": MAX_FILE_SIZE,
        }

        logger.info("Starting Instagram extraction.")

        # Download the video only once.
        with yt_dlp.YoutubeDL(options) as ydl:

            info = ydl.extract_info(
                url,
                download=True
            )

        video = find_video(folder)

        if video is None:
            raise RuntimeError("Downloaded video not found.")

        # Extract MP3 locally using FFmpeg.
        audio = folder / "audio.mp3"

        create_mp3(video, audio)

        response = {
            "title": info.get(
                "title",
                "Instagram Reel"
            ),
            "author": info.get("uploader", ""),
            "thumbnail": info.get("thumbnail", ""),
            "videoUrl": (
                f"{BACKEND_URL}/download/{job_id}/video"
            ),
            "audioUrl": (
                f"{BACKEND_URL}/download/{job_id}/audio"
            ),
        }

        save_cached_result(
            cache_key,
            job_id,
            response
        )

        logger.info(
            "Media processing completed successfully: %s",
            job_id
        )

        return response

    except HTTPException:

        if folder:
            shutil.rmtree(folder, ignore_errors=True)

        raise

    except Exception as error:

        logger.exception("Media processing failed.")

        if is_rate_limit_error(error):

            activate_cooldown()

            if folder:
                shutil.rmtree(folder, ignore_errors=True)

            raise HTTPException(
                status_code=429,
                detail=(
                    "Instagram is temporarily limiting requests. "
                    "Please wait before trying again."
                ),
                headers={
                    "Retry-After": str(RATE_LIMIT_SECONDS)
                }
            )

        if folder:
            shutil.rmtree(folder, ignore_errors=True)

        if isinstance(error, yt_dlp.utils.DownloadError):

            raise HTTPException(
                status_code=422,
                detail=(
                    "Unable to retrieve this Instagram media. "
                    "It may be unavailable or unsupported."
                )
            )

        raise HTTPException(
            status_code=500,
            detail="Media processing failed. Please try again later."
        )

    finally:

        PROCESS_LOCK.release()


# --------------------------------------------------
# VIDEO AND AUDIO DOWNLOAD
# --------------------------------------------------

@app.get("/download/{job_id}/{kind}")
def download(job_id: str, kind: str):

    if kind not in ("video", "audio"):

        raise HTTPException(
            status_code=400,
            detail="Invalid download type."
        )

    if (
        len(job_id) != 32
        or not all(c in "0123456789abcdef" for c in job_id)
    ):

        raise HTTPException(
            status_code=400,
            detail="Invalid download ID."
        )

    folder = BASE_DIR / job_id

    if not folder.exists():

        raise HTTPException(
            status_code=404,
            detail="Download expired or unavailable."
        )

    if kind == "video":

        file = find_video(folder)

        if file is None:
            raise HTTPException(404, "Video unavailable.")

        filename = "instagram_video" + file.suffix

        media_type = (
            "video/mp4"
            if file.suffix.lower() == ".mp4"
            else "application/octet-stream"
        )

    else:

        file = folder / "audio.mp3"

        if not file.exists():
            raise HTTPException(404, "Audio unavailable.")

        filename = "instagram_audio.mp3"
        media_type = "audio/mpeg"

    return FileResponse(
        path=file,
        filename=filename,
        media_type=media_type
    )
