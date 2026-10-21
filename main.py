
import os
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title="TikSave HD API")

class VideoRequest(BaseModel):
    url: str

def valid_tiktok_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        return (
            parsed.scheme == "https"
            and (host == "tiktok.com" or host.endswith(".tiktok.com"))
        )
    except ValueError:
        return False

@app.get("/")
def home():
    return {
        "status": "online",
        "service": "TikSave HD",
        "version": "1.0"
    }

@app.post("/api/download")
async def download_video(data: VideoRequest):
    if not valid_tiktok_url(data.url):
        raise HTTPException(
            status_code=400,
            detail="Insira um link HTTPS válido do TikTok."
        )

    try:
        async with httpx.AsyncClient(
            timeout=25,
            follow_redirects=True
        ) as client:
            response = await client.post(
                "https://ssstik.io/abc",
                data={
                    "id": data.url,
                    "locale": "pt",
                    "tt": ""
                },
                headers={
                    "User-Agent": "Mozilla/5.0",
                    "Accept": "text/html"
                }
            )
            response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")

        links = []
        for a in soup.select("a[href]"):
            href = a.get("href", "")
            if href.startswith("https://"):
                links.append({
                    "url": href,
                    "label": a.get_text(" ", strip=True)
                })

        if not links:
            raise HTTPException(
                status_code=422,
                detail="Nenhum link de download encontrado. O serviço pode exigir outro método."
            )

        return {
            "status": "ok",
            "links": links
        }

    except httpx.HTTPError:
        raise HTTPException(
            status_code=502,
            detail="Não foi possível consultar o serviço de download."
        )
