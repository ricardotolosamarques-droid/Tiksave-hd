import asyncio
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import urlparse
from fastapi import FastAPI, HTTPException, Request, BackgroundTasks
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


def _telegram_download(url: str, folder: str):
    """Download one MP4 video with audio; never choose audio-only formats."""
    opts = {
        "quiet": True, "no_warnings": True, "noplaylist": True,
        "socket_timeout": 20, "retries": 1,
        "format": "best[ext=mp4][vcodec!=none][acodec!=none]/best[vcodec!=none][acodec!=none][ext=mp4]",
        "outtmpl": str(Path(folder) / "video.%(ext)s"),
        "max_filesize": 45 * 1024 * 1024,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        path = Path(ydl.prepare_filename(info))
    if not path.is_file() or path.suffix.lower() != ".mp4":
        raise ValueError("Vídeo MP4 indisponível.")
    if path.stat().st_size > 45 * 1024 * 1024:
        raise ValueError("Vídeo excede o limite de envio.")
    return (info or {}).get("title") or "Vídeo do TikTok", path


async def _telegram_process_video(chat_id: int, url: str):
    try:
        with tempfile.TemporaryDirectory(prefix="tiksave_telegram_") as folder:
            title, path = await asyncio.to_thread(_telegram_download, url, folder)
            async with httpx.AsyncClient(timeout=120) as client:
                with path.open("rb") as video:
                    response = await client.post(
                        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendVideo",
                        data={"chat_id": str(chat_id), "caption": title[:900], "supports_streaming": "true"},
                        files={"video": ("video.mp4", video, "video/mp4")},
                    )
                response.raise_for_status()
                if not response.json().get("ok"):
                    raise ValueError("Telegram recusou o envio.")
    except Exception:
        try:
            await _telegram_send(chat_id, "Não consegui enviar esse vídeo. Pode estar indisponível, ser grande demais ou não ter um MP4 compatível.")
        except Exception:
            pass


@app.post("/api/telegram-webhook")
async def telegram_webhook(request: Request, background_tasks: BackgroundTasks):
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

    background_tasks.add_task(_telegram_process_video, chat_id, url)
    return {"status": "ok"}
