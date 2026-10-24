import os
import re
import time
import asyncio
import secrets
import ipaddress
import socket
from fastapi.responses import StreamingResponse
from urllib.parse import urlparse, urljoin
import httpx
import yt_dlp
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel
from highlights_adapter import normalize_collections, normalize_items

app = FastAPI(title="TikSave HD API", version="3.1.0")
SSSTIK_HOME = "https://ssstik.io/"
SSSTIK_ENDPOINT = "https://ssstik.io/abc?url=dl"
COLLECTIONS_TTL = 86400
ITEMS_TTL = 1800
RATE_LIMIT_WAIT = 300
cache = {}
download_tokens = {}
DOWNLOAD_TTL = 600
MAX_FILE_BYTES = 250 * 1024 * 1024
MEDIA_DOMAINS = ("tiktok.com", "tiktokcdn.com", "tiktokcdn-us.com", "byteoversea.com", "ibytedtos.com", "byteimg.com", "muscdn.com", "musical.ly")
rate_limit_until = 0

class VideoRequest(BaseModel):
    url: str

def valid_tiktok_url(value):
    try:
        p = urlparse(value)
        host = (p.hostname or "").lower()
        return p.scheme == "https" and (host == "tiktok.com" or host.endswith(".tiktok.com")) and not p.username and not p.password
    except (ValueError, TypeError):
        return False

def safe_media_url(value):
    if not isinstance(value, str):
        return None
    try:
        p = urlparse(value)
        if p.scheme == "https" and p.hostname and not p.username and not p.password:
            return value
    except (ValueError, TypeError):
        pass
    return None

def first_url(value):
    if isinstance(value, str):
        return safe_media_url(value)
    if isinstance(value, list):
        return next((v for x in value if (v := first_url(x))), None)
    if isinstance(value, dict):
        return next((v for k in ("url_list", "url", "uri") if (v := first_url(value.get(k)))), None)
    return None

def extract_media(story):
    if not isinstance(story, dict):
        return []
    results = []
    video = story.get("video") or {}
    if isinstance(video, dict):
        url = first_url(video.get("play_addr")) or first_url(video.get("download_addr")) or first_url(video.get("url"))
        if url:
            results.append({"type": "video", "url": url, "downloadAllowed": True})
    image_sources = []
    for field in ("images", "image_post_info"):
        value = story.get(field)
        if isinstance(value, dict):
            value = value.get("images", [])
        if isinstance(value, list):
            image_sources.extend(value)
    images = []
    for item in image_sources:
        if isinstance(item, dict):
            url = first_url(item.get("display_image")) or first_url(item.get("owner_watermark_image")) or first_url(item.get("url_list")) or first_url(item.get("url"))
        else:
            url = first_url(item)
        if url and url not in images:
            images.append(url)
    if images:
        results.append({"type": "image", "images": images, "downloadAllowed": True})
    return results

def clean_username(value):
    value = value.strip()
    if "tiktok.com/@" in value:
        value = value.split("tiktok.com/@", 1)[1].split("?", 1)[0].split("/", 1)[0]
    value = value.lstrip("@")
    if not re.fullmatch(r"[A-Za-z0-9._]{2,24}", value):
        raise HTTPException(status_code=400, detail="Nome de usuário inválido.")
    return value


# Rotas restauradas: download, proxy e destaques.
# Cobalt permanece independente no Cloudflare Worker.
from fastapi.responses import JSONResponse

def _extract_ytdlp(url):
    opts = {
        "quiet": True, "no_warnings": True, "skip_download": True,
        "noplaylist": True, "socket_timeout": 15, "retries": 1,
        "extractor_retries": 1, "cachedir": False,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    formats = (info or {}).get("formats") or [info]
    out = []
    for fmt in formats:
        if not isinstance(fmt, dict):
            continue
        media_url = safe_media_url(fmt.get("url"))
        if not media_url or fmt.get("vcodec") in ("none", None):
            continue
        if fmt.get("acodec") == "none":
            continue
        if fmt.get("protocol") not in (None, "http", "https"):
            continue
        out.append({
            "url": media_url, "source": "yt-dlp",
            "width": int(fmt.get("width") or 0),
            "height": int(fmt.get("height") or 0),
            "bitrate_kbps": float(fmt.get("tbr") or 0),
            "http_headers": fmt.get("http_headers") or info.get("http_headers") or {},
        })
    return out


def _allowed_cdn(url):
    try:
        p = urlparse(url)
        host = (p.hostname or "").lower().rstrip(".")
        return (p.scheme == "https" and p.port in (None, 443)
                and p.username is None and p.password is None
                and any(host == d or host.endswith("." + d) for d in MEDIA_DOMAINS))
    except (ValueError, TypeError):
        return False


def _public_dns(host):
    try:
        addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        return bool(addresses) and all(
            ipaddress.ip_address(a[4][0]).is_global for a in addresses
        )
    except (OSError, ValueError):
        return False


def _make_token(url, headers):
    if not _allowed_cdn(url):
        return None
    now = time.monotonic()
    for key, record in list(download_tokens.items()):
        if record[1] <= now:
            download_tokens.pop(key, None)
    token = secrets.token_urlsafe(32)
    allowed = {"user-agent", "referer", "origin", "accept", "accept-language"}
    clean = {k: v for k, v in (headers or {}).items()
             if isinstance(k, str) and isinstance(v, str) and k.lower() in allowed}
    download_tokens[token] = (url, now + DOWNLOAD_TTL, clean)
    return token


@app.get("/")
def health():
    return {"status": "online", "service": "TikSave HD", "version": "3.2.2"}


@app.post("/api/download")
async def download_video(data: VideoRequest):
    if not valid_tiktok_url(data.url):
        raise HTTPException(status_code=400, detail="Link HTTPS do TikTok inválido.")
    diagnostic = {}
    try:
        candidates = await asyncio.wait_for(
            asyncio.to_thread(_extract_ytdlp, data.url), timeout=35
        )
        diagnostic["yt_dlp"] = f"{len(candidates)}_formatos"
    except asyncio.TimeoutError:
        candidates = []
        diagnostic["yt_dlp"] = "tempo_esgotado"
    except Exception as exc:
        candidates = []
        diagnostic["yt_dlp"] = type(exc).__name__
    links = []
    seen = set()
    for fmt in sorted(
        candidates,
        key=lambda f: (f["width"] * f["height"], f["bitrate_kbps"]),
        reverse=True
    ):
        token = _make_token(fmt["url"], fmt["http_headers"])
        if not token:
            continue
        key = (fmt["width"], fmt["height"], round(fmt["bitrate_kbps"]))
        if key in seen:
            continue
        seen.add(key)
        width, height = fmt["width"], fmt["height"]
        links.append({
            "url": "/api/file?token=" + token,
            "label": f"yt-dlp • {width}x{height}" if width and height else "yt-dlp • qualidade não verificada",
            "source": "yt-dlp", "width": width or None,
            "height": height or None,
            "bitrate_kbps": fmt["bitrate_kbps"] or None,
            "quality_verified": bool(width and height),
        })
        if len(links) >= 12:
            break
    if not links:
        raise HTTPException(status_code=502, detail={
            "message": "yt-dlp não encontrou links de vídeo disponíveis.",
            "diagnostic": diagnostic
        })
    return {
        "status": "ok", "source": "yt-dlp", "links": links,
        "hd_available": any((link["height"] or 0) >= 720 for link in links),
        "preview_url": None, "diagnostic": diagnostic
    }


@app.get("/api/file")
async def download_file(token: str = Query(..., min_length=20)):
    record = download_tokens.get(token)
    if not record or record[1] < time.monotonic():
        raise HTTPException(status_code=410, detail="Link expirado. Busque novamente.")
    url, _, saved_headers = record
    if not _allowed_cdn(url) or not await asyncio.to_thread(_public_dns, urlparse(url).hostname):
        raise HTTPException(status_code=403, detail="CDN não permitida.")
    # curl_cffi usa fingerprint TLS de navegador, preservando headers do yt-dlp.
    # A validação de domínio e DNS continua obrigatória a cada redirect.
    from curl_cffi import requests as curl_requests

    def _open_video():
        session = curl_requests.Session(impersonate="chrome")
        response = None
        try:
            current = url
            headers = {"Accept": "video/mp4,*/*", **saved_headers}
            for _ in range(4):
                if not _allowed_cdn(current) or not _public_dns(urlparse(current).hostname):
                    raise HTTPException(status_code=403, detail="CDN não permitida.")
                response = session.get(
                    current, headers=headers, stream=True,
                    allow_redirects=False, timeout=30
                )
                if response.status_code not in (301, 302, 303, 307, 308):
                    break
                location = response.headers.get("location")
                response.close()
                response = None
                if not location:
                    raise HTTPException(status_code=502, detail="Redirecionamento inválido.")
                current = urljoin(current, location)
            else:
                raise HTTPException(status_code=502, detail="Redirecionamentos excessivos.")
            if response.status_code != 200:
                raise HTTPException(
                    status_code=502,
                    detail=f"CDN retornou HTTP {response.status_code} (curl_cffi)."
                )
            content_type = response.headers.get("content-type", "").lower()
            length = response.headers.get("content-length", "")
            if not (content_type.startswith("video/") or
                    content_type.startswith("application/octet-stream")):
                raise HTTPException(status_code=502, detail="CDN não retornou um vídeo.")
            if length.isdigit() and int(length) > MAX_FILE_BYTES:
                raise HTTPException(status_code=413, detail="Vídeo excede limite de tamanho.")
            return session, response
        except Exception:
            if response is not None:
                response.close()
            session.close()
            raise

    try:
        session, response = await asyncio.to_thread(_open_video)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail=f"Falha na conexão com a CDN ({type(exc).__name__})."
        )

    def _read_next(iterator):
        return next(iterator, None)

    async def stream():
        total = 0
        iterator = response.iter_content(chunk_size=65536)
        try:
            while True:
                chunk = await asyncio.to_thread(_read_next, iterator)
                if chunk is None:
                    break
                total += len(chunk)
                if total > MAX_FILE_BYTES:
                    break
                if chunk:
                    yield chunk
        finally:
            await asyncio.to_thread(response.close)
            await asyncio.to_thread(session.close)

    return StreamingResponse(stream(), media_type="video/mp4", headers={
        "Content-Disposition": 'attachment; filename="tiksave-video.mp4"',
        "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"
    })

@app.get("/api/highlights")
async def highlights(username: str = Query(..., min_length=1),
                     collection: str = Query(""), cursor: str = Query("0")):
    global rate_limit_until
    username = clean_username(username)
    if collection and not re.fullmatch(r"[0-9]{1,30}", collection):
        raise HTTPException(status_code=400, detail="Coleção inválida.")
    if not re.fullmatch(r"[0-9]{1,30}", cursor):
        raise HTTPException(status_code=400, detail="Cursor inválido.")
    api_url = os.getenv("HIGHLIGHTS_API_URL", "").strip()
    method = os.getenv("HIGHLIGHTS_API_METHOD", "GET").upper()
    if not api_url.startswith("https://") or method not in ("GET", "POST"):
        raise HTTPException(status_code=503, detail="Fonte de Destaques não configurada.")
    key = (username, collection, cursor)
    record = cache.get(key)
    if record and record[0] > time.monotonic():
        return record[1]
    if time.monotonic() < rate_limit_until:
        raise HTTPException(status_code=503, detail="Provedor temporariamente limitado.")
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
            if method == "POST":
                payload = ({"action": "posts", "highlightId": collection, "cursor": cursor}
                           if collection else {"username": username})
                response = await client.post(api_url, json=payload)
            else:
                params = {"username": username}
                if collection:
                    params.update({"collection": collection, "cursor": cursor})
                response = await client.get(api_url, params=params)
            if response.status_code == 429:
                rate_limit_until = time.monotonic() + RATE_LIMIT_WAIT
                raise HTTPException(status_code=503, detail="Limite do provedor atingido.")
            response.raise_for_status()
            payload = response.json()
        result = normalize_items(payload) if collection else normalize_collections(payload)
        if collection:
            result.update({"username": username, "collection": collection})
        cache[key] = (time.monotonic() + (ITEMS_TTL if collection else COLLECTIONS_TTL), result)
        return result
    except HTTPException:
        raise
    except (httpx.HTTPError, ValueError, TypeError):
        raise HTTPException(status_code=502, detail="Falha ao consultar Destaques.")
