import asyncio
import os
import re
from urllib.parse import urlparse
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
import httpx
import yt_dlp

app = FastAPI(title="TikSave HD Backend", version="2.1")

# Dicionário de tokens para o proxy (ajuste conforme a sua implementação atual)
download_tokens = {}

@app.post("/api/download")
async def api_download(payload: dict):
    url = payload.get("url")
    if not url:
        raise HTTPException(status_code=400, detail="URL não fornecida.")
    
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
    }
    
    def extract():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(url, download=False)
            
    try:
        info = await asyncio.to_thread(extract)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro ao extrair vídeo: {str(e)}")
        
    formats = []
    for f in info.get('formats', []):
        # BLOQUEIO DE ÁUDIO: Ignora formatos que possuem apenas áudio (sem vídeo)
        if f.get('vcodec') == 'none':
            continue
            
        formats.append({
            "format_id": f.get("format_id"),
            "ext": f.get("ext"),
            "url": f.get("url"),
            "resolution": f.get("resolution") or f"{f.get('width', 0)}x{f.get('height', 0)}"
        })
        
    return {
        "success": True,
        "title": info.get("title"),
        "formats": formats
    }

@app.get("/api/file")
async def proxy_file(token: str):
    target_url = download_tokens.get(token)
    if not target_url:
        raise HTTPException(status_code=404, detail="Token inválido ou expirado.")

    async with httpx.AsyncClient(follow_redirects=True) as client:
        try:
            response = await client.get(target_url, headers={"Range": "bytes=0-1023"}, timeout=10.0)
            content_type = response.headers.get("content-type", "").lower()
            
            # BLOQUEIO DE ÁUDIO NO PROXY: Rejeita se o Content-Type for de áudio
            if "audio/" in content_type:
                raise HTTPException(
                    status_code=403, 
                    detail="O descarregamento de ficheiros de áudio não é permitido."
                )
        except Exception as e:
            if isinstance(e, HTTPException):
                raise e
            raise HTTPException(status_code=500, detail="Erro ao validar arquivo.")
            
    # Realiza o streaming do vídeo aprovado
    async def stream_generator():
        async with httpx.AsyncClient(follow_redirects=True) as c:
            async with c.stream("GET", target_url) as r:
                async for chunk in r.aiter_bytes():
                    yield chunk

    return StreamingResponse(stream_generator(), media_type="video/mp4")

@app.get("/api/highlights")
async def get_highlights(username: str):
    clean_user = username.strip("@")
    profile_url = f"https://www.tiktok.com/@{clean_user}"
    
    ydl_opts = {
        'extract_flat': True,
        'skip_download': True,
        'quiet': True,
    }
    
    def extract_profile():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(profile_url, download=False)
            
    try:
        info = await asyncio.to_thread(extract_profile)
        entries = info.get('entries', [])
        
        highlights = []
        for entry in entries[:20]:
            highlights.append({
                "id": entry.get("id"),
                "title": entry.get("title", ""),
                "url": entry.get("url") or f"https://www.tiktok.com/@{clean_user}/video/{entry.get('id')}",
                "thumbnail": entry.get("thumbnail"),
                "duration": entry.get("duration"),
            })
            
        return {"success": True, "highlights": highlights}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro ao buscar destaques: {str(e)}")


# Telegram bot integration. Configure both secrets in Render, never in GitHub.
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip()


def _tiktok_link_from_message(message_text: str):
    """Accept only an HTTPS TikTok URL, not arbitrary yt-dlp targets."""
    for raw in re.findall(r"https://[^\\s<>]+", message_text):
        candidate = raw.rstrip(".,!?)")
        try:
            parsed = urlparse(candidate)
            host = (parsed.hostname or "").lower().rstrip(".")
            if (parsed.scheme == "https" and parsed.port in (None, 443)
                    and parsed.username is None and parsed.password is None
                    and (host == "tiktok.com" or host.endswith(".tiktok.com"))):
                return candidate
        except ValueError:
            continue
    return None


async def _telegram_send(chat_id: int, message: str):
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": message, "disable_web_page_preview": True},
        )
        response.raise_for_status()


def _telegram_extract(url: str):
    opts = {
        "quiet": True, "no_warnings": True, "skip_download": True,
        "noplaylist": True, "socket_timeout": 15, "retries": 1,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    candidates = []
    for fmt in (info or {}).get("formats") or [info]:
        if not isinstance(fmt, dict):
            continue
        media_url = fmt.get("url")
        if (not isinstance(media_url, str) or not media_url.startswith("https://")
                or fmt.get("vcodec") in ("none", None)
                or str(fmt.get("ext") or "").lower() in {"mp3", "m4a", "aac", "ogg", "opus"}):
            continue
        # Prefer a single playable video+audio stream when available.
        candidates.append(fmt)
    candidates.sort(key=lambda f: (
        f.get("acodec") not in ("none", None),
        int(f.get("height") or 0),
        float(f.get("tbr") or 0),
    ), reverse=True)
    return (info or {}).get("title") or "Vídeo do TikTok", (
        candidates[0].get("url") if candidates else None
    )


@app.post("/api/telegram-webhook")
async def telegram_webhook(request: Request):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_WEBHOOK_SECRET:
        raise HTTPException(status_code=503, detail="Bot do Telegram não configurado.")
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != TELEGRAM_WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Webhook não autorizado.")
    data = await request.json()
    message = data.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    message_text = message.get("text") or ""
    if not chat_id or not isinstance(message_text, str):
        return {"status": "ok"}

    if message_text.strip().startswith("/start"):
        await _telegram_send(chat_id, "Olá! Envie um link do TikTok para receber uma opção de vídeo.")
        return {"status": "ok"}

    url = _tiktok_link_from_message(message_text)
    if not url:
        return {"status": "ok"}

    await _telegram_send(chat_id, "Processando o link do TikTok...")
    try:
        title, video_url = await asyncio.to_thread(_telegram_extract, url)
        if video_url:
            # CDN links may expire; absence of watermark is not guaranteed.
            await _telegram_send(chat_id, f"{title[:150]}\\n\\nLink do vídeo (temporário):\\n{video_url}")
        else:
            await _telegram_send(chat_id, "Não encontrei um formato de vídeo válido para esse link.")
    except Exception:
        await _telegram_send(chat_id, "Não foi possível processar esse vídeo agora.")
    return {"status": "ok"}
