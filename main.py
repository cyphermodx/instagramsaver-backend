from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from yt_dlp import YoutubeDL

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.post("/")
async def get_video(data: dict):
    url = data.get("url")

    if not url or "instagram.com" not in url.lower():
        raise HTTPException(status_code=400, detail="Invalid Instagram URL")

    try:
        options = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": True,
        }

        with YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=False)

       BACKEND_URL = "https://instagramsaver-backend.onrender.com"

return {
    "title": info.get("title", "Instagram Reel"),
    "author": info.get("uploader", ""),
    "thumbnail": info.get("thumbnail", ""),
    "videoUrl": f"{BACKEND_URL}/download/{job_id}/video",
    "audioUrl": f"{BACKEND_URL}/download/{job_id}/audio",
}

    except Exception:
        raise HTTPException(
            status_code=400,
            detail="Unable to retrieve this video. It may be unavailable."
        )
