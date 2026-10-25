import asyncio
import os
import re
import tempfile
import secrets
import time
from pathlib import Path
from urllib.parse import urlparse
from fastapi import FastAPI, HTTPException, Request, BackgroundTasks
from fastapi.responses import StreamingResponse
import httpx
import yt_dlp

app = FastAPI(title="TikSave HD Backend", version="2.1")

# Dicionário de tokens para o proxy (ajuste conforme a sua implementação atual)
download_tokens = {}  # token -> (media URL, expiry timestamp)

@app.get("/")
def home():
    return {"status": "online", "service": "TikSave HD", "version": "2.2"}


def _site_extract_yt_dlp(url: str):
    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "noplaylist": True, "skip_download": True}) as ydl:
        info = ydl.extract_info(url, download=False)
    candidates = []
    for fmt in (info.get("formats") or [info]):
        if fmt.get("vcodec") == "none" or fmt.get("acodec") == "none":
            continue
        if fmt.get("ext") != "mp4" or not fmt.get("url"):
            continue
        candidates.append(fmt)
    candidates.sort(key=lambda f: (f.get("height") or 0, f.get("tbr") or 0), reverse=True)
    if not candidates:
        raise ValueError("yt-dlp não encontrou MP4 com áudio.")
    return [{"url": f["url"], "label": f"yt-dlp • {f.get('height') or '?'}p",
             "source": "yt-dlp", "width": f.get("width"), "height": f.get("height"),
             "bitrate_kbps": f.get("tbr"), "quality_verified": False}
            for f in candidates[:3]]


def _site_extract_tikwm(url: str):
    from urllib.parse import urljoin
    with httpx.Client(timeout=25) as client:
        response = client.get("https://www.tikwm.com/api/", params={"url": url, "hd": 1})
        response.raise_for_status()
        payload = response.json()
    data = payload.get("data") or {}
    if payload.get("code") != 0 or not isinstance(data, dict):
        raise ValueError("TikWM indisponível.")
    links = []
    for key, label in (("hdplay", "TikWM • HD"), ("play", "TikWM • Sem marca d'água")):
        media = data.get(key)
        if media:
            media = urljoin("https://www.tikwm.com/", media)
            if media.startswith("https://") and media not in [v["url"] for v in links]:
                links.append({"url": media, "label": label, "source": "tikwm", "quality_verified": False})
    return links


# Cobalt requires a working API instance, configured in Render.
COBALT_API_URL = os.getenv("COBALT_API_URL", "").strip().rstrip("/")
COBALT_API_KEY = os.getenv("COBALT_API_KEY", "").strip()


def _site_extract_cobalt(url: str):
    if not COBALT_API_URL:
        raise ValueError("COBALT_API_URL não configurada no Render.")
    parsed = urlparse(COBALT_API_URL)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("COBALT_API_URL deve ser HTTPS.")
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if COBALT_API_KEY:
        headers["Authorization"] = f"Api-Key {COBALT_API_KEY}"
    with httpx.Client(timeout=35, follow_redirects=False) as client:
        response = client.post(
            COBALT_API_URL,
            json={"url": url, "videoQuality": "max", "filenameStyle": "basic"},
            headers=headers,
        )
        if response.is_error:
            print("Cobalt API HTTP", response.status_code, "response:", response.text[:650], flush=True)
            raise ValueError(f"Cobalt recusou a solicitação (HTTP {response.status_code}).")
        payload = response.json()
    status = payload.get("status")
    candidates = []
    if status in ("tunnel", "redirect") and payload.get("url"):
        candidates.append(payload["url"])
    elif status == "picker":
        for item in payload.get("picker") or []:
            if item.get("type") == "video" and item.get("url"):
                candidates.append(item["url"])
    if not candidates:
        raise ValueError(f"Cobalt não retornou vídeo (status={status}).")
    return [{"url": media, "label": "Cobalt • Vídeo", "source": "cobalt",
             "quality_verified": False} for media in candidates[:3]
            if urlparse(media).scheme == "https"]


@app.post("/api/download")
async def api_download(payload: dict):
    url = payload.get("url", "")
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or not (host == "tiktok.com" or host.endswith(".tiktok.com")):
            raise ValueError()
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Insira um link HTTPS válido do TikTok.")
    # Always try both providers so the UI can offer real alternatives.
    results = await asyncio.gather(
        asyncio.to_thread(_site_extract_tikwm, url),
        asyncio.to_thread(_site_extract_yt_dlp, url),
        asyncio.to_thread(_site_extract_cobalt, url),
        return_exceptions=True,
    )
    links = []
    for source, result in zip(("tikwm", "yt-dlp", "cobalt"), results):
        if isinstance(result, Exception):
            print(f"Site {source} failed: {type(result).__name__}: {str(result)[:160]}")
            continue
        for item in result:
            media = item["url"]
            parsed_media = urlparse(media)
            if parsed_media.scheme != "https" or not parsed_media.hostname:
                continue
            # Do not expose provider URLs to the frontend: proxy via expiring token.
            token = secrets.token_urlsafe(24)
            download_tokens[token] = (media, time.time() + 900)
            links.append({**item, "url": f"/api/file?token={token}"})
    if not links:
        raise HTTPException(status_code=502, detail="Nenhuma fonte conseguiu obter o vídeo.")
    # Avoid retaining tokens indefinitely on a long-running server.
    now = time.time()
    for token, (_, expiry) in list(download_tokens.items()):
        if expiry < now:
            download_tokens.pop(token, None)
    return {"status": "ok", "source": links[0]["source"], "links": links}


@app.get("/api/file")
async def proxy_file(token: str):
    import ipaddress
    import socket
    from urllib.parse import urljoin

    entry = download_tokens.get(token)
    if not entry or entry[1] < time.time():
        raise HTTPException(status_code=404, detail="Link expirado. Faça nova busca.")

    def safe_url(value):
        parsed = urlparse(value)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or not host or parsed.username or parsed.password:
            return False
        allowed = (
            "tikwm.com", "tiktokcdn.com", "tiktokcdn-us.com",
            "byteoversea.com", "byteoversea.net", "ibytedtos.com",
            "tiktokv.com", "tiktok.com", "muscdn.com",
            "cobalt.tools", "cobalt-api.me", "cobaltcdn.com",
        )
        cobalt_host = (urlparse(COBALT_API_URL).hostname or "").lower()
        if cobalt_host:
            allowed += (cobalt_host,)
        if not any(host == domain or host.endswith("." + domain) for domain in allowed):
            return False
        try:
            addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
            return bool(addresses) and all(ipaddress.ip_address(a[4][0]).is_global for a in addresses)
        except OSError:
            return False

    # Validate the upstream BEFORE sending HTTP 200 to the browser.
    # Previously exceptions inside StreamingResponse caused broken partial downloads.
    client = httpx.AsyncClient(timeout=90, follow_redirects=False)
    stream_context = None
    response = None
    current = entry[0]
    try:
        for _ in range(5):
            if not await asyncio.to_thread(safe_url, current):
                raise HTTPException(status_code=502, detail="Fonte retornou domínio de mídia não autorizado.")
            stream_context = client.stream("GET", current, headers={
                "User-Agent": "Mozilla/5.0",
                "Accept": "video/mp4,application/octet-stream,*/*",
            })
            response = await stream_context.__aenter__()
            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("location")
                await stream_context.__aexit__(None, None, None)
                stream_context = None
                if not location:
                    raise HTTPException(status_code=502, detail="Redirecionamento inválido.")
                current = urljoin(current, location)
                continue
            if response.status_code >= 400:
                print(f"Site proxy upstream failed: HTTP {response.status_code} host={urlparse(current).hostname}")
                raise HTTPException(status_code=502, detail=f"Fonte recusou o vídeo (HTTP {response.status_code}).")
            mime = response.headers.get("content-type", "").split(";")[0].lower()
            if mime.startswith("audio/") or mime in ("text/html", "application/json"):
                raise HTTPException(status_code=502, detail="Fonte não retornou arquivo de vídeo.")
            break
        else:
            raise HTTPException(status_code=502, detail="Muitos redirecionamentos.")
    except HTTPException:
        if stream_context:
            await stream_context.__aexit__(None, None, None)
        await client.aclose()
        raise
    except (httpx.HTTPError, OSError) as exc:
        if stream_context:
            await stream_context.__aexit__(None, None, None)
        await client.aclose()
        print(f"Site proxy connection failed: {type(exc).__name__}: {str(exc)[:150]}")
        raise HTTPException(status_code=502, detail="Falha ao conectar à fonte de vídeo.")

    async def video_stream():
        try:
            async for chunk in response.aiter_bytes():
                yield chunk
        finally:
            await stream_context.__aexit__(None, None, None)
            await client.aclose()

    headers = {"Content-Disposition": 'attachment; filename="tiksave.mp4"'}
    if response.headers.get("content-length"):
        headers["Content-Length"] = response.headers["content-length"]
    return StreamingResponse(video_stream(), media_type="video/mp4", headers=headers)


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


def _telegram_link_from_message(message_text: str):
    """Allow only HTTPS links from explicitly supported social platforms."""
    platforms = {
        "tiktok": ("tiktok.com",),
        "instagram": ("instagram.com",),
        "youtube": ("youtube.com", "youtu.be"),
        "facebook": ("facebook.com", "fb.watch"),
        "twitter": ("twitter.com", "x.com"),
        "reddit": ("reddit.com", "redd.it"),
        "pinterest": ("pinterest.com", "pin.it"),
    }
    for raw in re.findall(r"https://[^\s<>]+", message_text):
        candidate = raw.rstrip(".,!?)")
        try:
            parsed = urlparse(candidate)
            host = (parsed.hostname or "").lower().rstrip(".")
            if parsed.scheme != "https" or parsed.port not in (None, 443) or parsed.username or parsed.password:
                continue
            for platform, domains in platforms.items():
                if any(host == domain or host.endswith("." + domain) for domain in domains):
                    return candidate, platform
        except ValueError:
            continue
    return None, None


async def _telegram_send(chat_id: int, message: str):
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": message, "disable_web_page_preview": True},
        )
        response.raise_for_status()


# Short opaque callback IDs avoid Telegram's 64-byte callback_data limit.
telegram_choices = {}  # choice ID -> (chat ID, TikTok URL, expiry)
TELEGRAM_CHOICE_TTL = 1800


async def _telegram_show_choices(chat_id: int, url: str, platform: str):
    now = time.time()
    for key, item in list(telegram_choices.items()):
        if item[2] < now:
            telegram_choices.pop(key, None)
    choice_id = secrets.token_urlsafe(12)
    telegram_choices[choice_id] = (chat_id, url, now + TELEGRAM_CHOICE_TTL)
    buttons = [{"text": "Cobalt", "callback_data": f"dl:cobalt:{choice_id}"}]
    if platform == "tiktok":
        buttons.insert(0, {"text": "TikWM", "callback_data": f"dl:tikwm:{choice_id}"})
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": "Escolha uma opção:",
                "reply_markup": {"inline_keyboard": [buttons]},
            },
        )
        response.raise_for_status()


async def _telegram_answer_callback(callback_id: str, text: str = ""):
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery",
            json={"callback_query_id": callback_id, "text": text[:180]},
        )
        response.raise_for_status()


TELEGRAM_MAX_VIDEO_BYTES = 45 * 1024 * 1024


def _telegram_download_yt_dlp(url: str, folder: str):
    opts = {
        "quiet": True, "no_warnings": True, "noplaylist": True,
        "socket_timeout": 20, "retries": 1,
        "format": "best[ext=mp4][vcodec!=none][acodec!=none]/best[vcodec!=none][acodec!=none][ext=mp4]",
        "outtmpl": str(Path(folder) / "video.%(ext)s"),
        "max_filesize": TELEGRAM_MAX_VIDEO_BYTES,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        path = Path(ydl.prepare_filename(info))
    if not path.is_file() or path.suffix.lower() != ".mp4":
        raise ValueError("yt-dlp não retornou MP4.")
    if path.stat().st_size > TELEGRAM_MAX_VIDEO_BYTES:
        raise ValueError("MP4 excede 45 MB.")
    return (info or {}).get("title") or "Vídeo do TikTok", path





def _telegram_download_youtube_clients(url: str, folder: str):
    """Try alternate public YouTube player clients without login or cookies.

    This is a best-effort fallback; it cannot overcome server-side IP verification.
    """
    last_error = None
    for client in ("android_vr", "web_safari"):
        try:
            target = Path(folder) / ("youtube_" + client)
            target.mkdir(parents=True, exist_ok=True)
            options = {
                "quiet": True, "no_warnings": True, "noplaylist": True,
                "socket_timeout": 20, "retries": 1,
                "extractor_args": {"youtube": {"player_client": [client]}},
                "format": "best[ext=mp4][vcodec!=none][acodec!=none]",
                "outtmpl": str(target / "video.%(ext)s"),
                "max_filesize": TELEGRAM_MAX_VIDEO_BYTES,
            }
            with yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(url, download=True)
                path = Path(ydl.prepare_filename(info))
            if not path.is_file() or path.suffix.lower() != ".mp4":
                raise ValueError("Cliente YouTube não retornou MP4.")
            if not 1024 <= path.stat().st_size <= TELEGRAM_MAX_VIDEO_BYTES:
                raise ValueError("Arquivo fora do limite de 45 MB.")
            with path.open("rb") as media:
                if b"ftyp" not in media.read(16):
                    raise ValueError("Arquivo não é MP4.")
            return (info or {}).get("title") or "Vídeo do YouTube", path
        except Exception as error:
            last_error = error
            print(f"Telegram YouTube client {client} failed: {type(error).__name__}: {str(error)[:180]}", flush=True)
    raise ValueError("Clientes alternativos do YouTube indisponíveis.") from last_error


def _telegram_download_pytubefix(url: str, folder: str):
    """Try public YouTube video with pytubefix, without account authentication."""
    from pytubefix import YouTube
    from pathlib import Path

    yt = YouTube(url)
    streams = yt.streams.filter(progressive=True, file_extension="mp4")
    candidates = sorted(
        (stream for stream in streams if stream.filesize and stream.filesize <= TELEGRAM_MAX_VIDEO_BYTES),
        key=lambda stream: stream.resolution or "",
        reverse=True,
    )
    if not candidates:
        raise ValueError("pytubefix não encontrou MP4 com áudio dentro do limite de 45 MB.")
    target = Path(folder) / "pytubefix"
    target.mkdir(parents=True, exist_ok=True)
    path = Path(candidates[0].download(output_path=str(target), filename="youtube.mp4"))
    if not path.is_file() or not 1024 <= path.stat().st_size <= TELEGRAM_MAX_VIDEO_BYTES:
        raise ValueError("pytubefix não retornou MP4 válido dentro do limite.")
    with path.open("rb") as file:
        if b"ftyp" not in file.read(16):
            raise ValueError("pytubefix retornou arquivo que não é MP4.")
    return (yt.title or "Vídeo do YouTube"), path


def _telegram_download_parth_dl(url: str, folder: str):
    """Try public Instagram extraction using parth-dl without authentication."""
    import subprocess
    import sys

    output_dir = Path(folder) / "parth_dl"
    output_dir.mkdir(parents=True, exist_ok=True)
    command = ["parth-dl", "--paths", str(output_dir), "--no-banner", "--quiet", url]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=100, check=False, cwd=str(output_dir))
    except subprocess.TimeoutExpired as error:
        raise ValueError("parth-dl excedeu o tempo limite.") from error
    media = sorted((p for p in output_dir.rglob("*") if p.is_file() and p.suffix.lower() in (".mp4", ".jpg", ".jpeg", ".png")), key=lambda p: str(p))
    if not media:
        print(f"Telegram parth-dl failed: exit={result.returncode}; {(result.stderr or result.stdout)[-300:]}", flush=True)
        raise ValueError("parth-dl não conseguiu obter mídia pública.")
    if len(media) > 10:
        raise ValueError("Álbum com mais de 10 arquivos não suportado.")
    for item in media:
        if not 1024 <= item.stat().st_size <= TELEGRAM_MAX_VIDEO_BYTES:
            raise ValueError("Mídia vazia ou excede 45 MB.")
        with item.open("rb") as f:
            signature = f.read(16)
        if item.suffix.lower() == ".mp4" and b"ftyp" not in signature:
            raise ValueError("Arquivo MP4 inválido.")
        if item.suffix.lower() in (".jpg", ".jpeg") and signature[:2] != bytes([255, 216]):
            raise ValueError("Arquivo JPEG inválido.")
        if item.suffix.lower() == ".png" and signature[:4] != bytes([137, 80, 78, 71]):
            raise ValueError("Arquivo PNG inválido.")
    return "Instagram • parth-dl", media if len(media) > 1 else media[0]


def _telegram_download_gallery_dl(url: str, folder: str):
    """Download public Instagram video posts using gallery-dl, without cookies."""
    import subprocess
    import sys

    output_dir = Path(folder) / "gallery_dl"
    output_dir.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable, "-m", "gallery_dl",
        "--directory", str(output_dir),
        "--filter", "extension == 'mp4'",
        url,
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=100, check=False)
    except subprocess.TimeoutExpired as error:
        raise ValueError("gallery-dl demorou demais para acessar o Instagram.") from error
    videos = sorted((p for p in output_dir.rglob("*") if p.is_file() and p.suffix.lower() == ".mp4"), key=lambda p: p.stat().st_size, reverse=True)
    if not videos:
        print(f"Telegram Instagram gallery-dl failed: exit={result.returncode}; {(result.stderr or result.stdout)[-350:]}", flush=True)
        if "redirect to login" in (result.stderr or "").lower() or "accounts/login" in (result.stderr or "").lower():
            raise ValueError("Instagram exige login para acessar este conteúdo pelo servidor.")
        raise ValueError("gallery-dl não encontrou vídeo MP4 público.")
    video = videos[0]
    if video.stat().st_size > TELEGRAM_MAX_VIDEO_BYTES:
        raise ValueError("Vídeo do Instagram excede 45 MB.")
    if video.stat().st_size < 1024:
        raise ValueError("gallery-dl retornou arquivo vazio.")
    with video.open("rb") as f:
        if b"ftyp" not in f.read(16):
            raise ValueError("gallery-dl não retornou MP4 válido.")
    return "Vídeo do Instagram • gallery-dl", video


def _telegram_download_tikwm(url: str, folder: str):
    """Fallback for public TikTok videos. Reject photo posts and audio-only links."""
    from urllib.parse import urljoin
    import ipaddress
    import socket

    with httpx.Client(timeout=30, follow_redirects=False) as client:
        response = client.get("https://www.tikwm.com/api/", params={"url": url, "hd": 1})
        response.raise_for_status()
        payload = response.json()
        data = payload.get("data") or {}
        if not isinstance(data, dict) or payload.get("code") != 0:
            raise ValueError("TikWM não encontrou vídeo.")
        if data.get("images") or data.get("image_post_info"):
            raise ValueError("Publicação de fotos, não de vídeo.")
        media_url = data.get("hdplay") or data.get("play")
        if not media_url:
            raise ValueError("TikWM retornou somente áudio ou nenhum vídeo.")
        media_url = urljoin("https://www.tikwm.com/", media_url)
        path = Path(folder) / "video.mp4"
        # Follow at most three redirects; reject non-HTTPS and private-network targets.
        for _ in range(4):
            parsed = urlparse(media_url)
            hostname = (parsed.hostname or "").lower()
            if parsed.scheme != "https" or not hostname or parsed.username or parsed.password:
                raise ValueError("URL de mídia inválida.")
            if not (hostname == "tikwm.com" or hostname.endswith(".tikwm.com")
                    or hostname == "tiktokcdn.com" or hostname.endswith(".tiktokcdn.com")
                    or hostname.endswith(".tiktokcdn-us.com")
                    or hostname.endswith(".byteoversea.com")
                    or hostname.endswith(".byteoversea.net")
                    or hostname.endswith(".ibytedtos.com")):
                raise ValueError("Domínio de mídia não permitido.")
            addresses = socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
            if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
                raise ValueError("Destino de rede não público.")
            with client.stream("GET", media_url, headers={"Accept": "video/mp4"}) as media:
                if media.status_code in (301, 302, 303, 307, 308):
                    location = media.headers.get("location")
                    if not location:
                        raise ValueError("Redirecionamento inválido.")
                    media_url = urljoin(media_url, location)
                    continue
                media.raise_for_status()
                mime = media.headers.get("content-type", "").split(";")[0].strip().lower()
                if mime.startswith("audio/") or mime in ("application/vnd.apple.mpegurl", "application/x-mpegurl"):
                    raise ValueError("Resposta não contém vídeo MP4.")
                total = 0
                with path.open("wb") as output:
                    for chunk in media.iter_bytes(chunk_size=65536):
                        total += len(chunk)
                        if total > TELEGRAM_MAX_VIDEO_BYTES:
                            raise ValueError("Vídeo excede 45 MB.")
                        output.write(chunk)
                if total < 1024:
                    raise ValueError("Arquivo de vídeo vazio.")
                with path.open("rb") as check:
                    header = check.read(32)
                if b"ftyp" not in header[:16]:
                    raise ValueError("Arquivo não é MP4.")
                return data.get("title") or "Vídeo do TikTok", path
        raise ValueError("Muitos redirecionamentos.")


def _telegram_download_cobalt(url: str, folder: str):
    import ipaddress
    import socket
    from urllib.parse import urljoin
    results = _site_extract_cobalt(url)
    if not results:
        raise ValueError("Cobalt não retornou vídeo.")
    media_url = results[0]["url"]
    path = Path(folder) / "cobalt_video.mp4"
    with httpx.Client(timeout=60, follow_redirects=False) as client:
        for _ in range(5):
            parsed = urlparse(media_url)
            host = (parsed.hostname or "").lower()
            if parsed.scheme != "https" or not host or parsed.username or parsed.password:
                raise ValueError("URL de mídia inválida.")
            addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
            if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
                raise ValueError("Destino de mídia não público.")
            with client.stream("GET", media_url) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get("location")
                    if not location:
                        raise ValueError("Redirecionamento sem destino.")
                    media_url = urljoin(media_url, location)
                    continue
                response.raise_for_status()
                if response.headers.get("content-type", "").lower().startswith("audio/"):
                    raise ValueError("Cobalt retornou áudio.")
                total = 0
                with path.open("wb") as output:
                    for chunk in response.iter_bytes(chunk_size=65536):
                        total += len(chunk)
                        if total > TELEGRAM_MAX_VIDEO_BYTES:
                            raise ValueError("Vídeo excede 45 MB.")
                        output.write(chunk)
                if total < 1024:
                    raise ValueError("Vídeo vazio.")
                with path.open("rb") as f:
                    if b"ftyp" not in f.read(16):
                        raise ValueError("Cobalt não retornou MP4.")
                return "Vídeo • Cobalt", path
    raise ValueError("Muitos redirecionamentos.")


def _telegram_download(url: str, folder: str):
    for name, downloader in (
        ("yt-dlp", _telegram_download_yt_dlp),
        ("TikWM", _telegram_download_tikwm),
        ("Cobalt", _telegram_download_cobalt),
    ):
        try:
            return downloader(url, folder)
        except Exception as error:
            print(f"Telegram {name} failed: {type(error).__name__}: {str(error)[:200]}")
    raise ValueError("Nenhuma fonte conseguiu obter um vídeo MP4 válido.")


async def _telegram_process_video(chat_id: int, url: str, provider: str):
    try:
        with tempfile.TemporaryDirectory(prefix="tiksave_telegram_") as folder:
            downloader = {"tikwm": _telegram_download_tikwm, "cobalt": _telegram_download_cobalt}[provider]
            try:
                title, path = await asyncio.to_thread(downloader, url, folder)
            except Exception as cobalt_error:
                host = (urlparse(url).hostname or "").lower()
                if provider != "cobalt" or not (host == "instagram.com" or host.endswith(".instagram.com") or host in ("youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be")):
                    raise
                print(f"Telegram Cobalt failed for {host}; trying yt-dlp: {type(cobalt_error).__name__}", flush=True)
                try:
                    if host == "instagram.com" or host.endswith(".instagram.com"):
                        try:
                            title, path = await asyncio.to_thread(_telegram_download_parth_dl, url, folder)
                        except Exception as parth_error:
                            print(f"Telegram Instagram parth-dl failed: {type(parth_error).__name__}: {str(parth_error)[:180]}", flush=True)
                            try:
                                title, path = await asyncio.to_thread(_telegram_download_gallery_dl, url, folder)
                            except Exception as gallery_error:
                                print(f"Telegram Instagram gallery-dl failed: {type(gallery_error).__name__}: {str(gallery_error)[:180]}", flush=True)
                                title, path = await asyncio.to_thread(_telegram_download_yt_dlp, url, folder)
                    else:
                        try:
                            title, path = await asyncio.to_thread(_telegram_download_yt_dlp, url, folder)
                        except Exception as youtube_yt_error:
                            print(f"Telegram YouTube yt-dlp failed: {type(youtube_yt_error).__name__}: {str(youtube_yt_error)[:180]}", flush=True)
                            try:
                                title, path = await asyncio.wait_for(
                                    asyncio.to_thread(_telegram_download_youtube_clients, url, folder), timeout=100
                                )
                            except Exception as client_error:
                                print(f"Telegram YouTube alternate clients failed: {type(client_error).__name__}", flush=True)
                                title, path = await asyncio.wait_for(
                                    asyncio.to_thread(_telegram_download_pytubefix, url, folder), timeout=100
                                )
                except Exception as fallback_error:
                    print(f"Telegram alternative download failed for {host}: {type(fallback_error).__name__}: {str(fallback_error)[:180]}", flush=True)
                    if host == "instagram.com" or host.endswith(".instagram.com"):
                        if "/stories/" in urlparse(url).path.lower():
                            story_parts = [part for part in urlparse(url).path.split("/") if part]
                            story_id = story_parts[2] if len(story_parts) >= 3 else "unknown"
                            print(f"Telegram Instagram Story unavailable: story_id={story_id}; cause={type(fallback_error).__name__}", flush=True)
                            raise ValueError("Não foi possível acessar este Story pelo servidor. Mesmo em perfis públicos, o Instagram pode exigir login para Stories. Reels e publicações continuam disponíveis.") from fallback_error
                        raise ValueError("O Instagram está bloqueando o acesso automático do servidor. Cobalt, parth-dl, gallery-dl e yt-dlp não conseguiram extrair o vídeo.") from fallback_error
                    if host in ("youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"):
                        raise ValueError("O YouTube está exigindo verificação ou login do servidor; Cobalt, yt-dlp e pytubefix falharam.") from fallback_error
                    raise ValueError("Nenhum extrator conseguiu acessar o vídeo.") from fallback_error
            import mimetypes
            from contextlib import ExitStack
            media_paths = path if isinstance(path, list) else [path]
            async with httpx.AsyncClient(timeout=120) as client:
                if len(media_paths) == 1:
                    item = media_paths[0]
                    is_video = item.suffix.lower() == ".mp4"
                    method = "sendVideo" if is_video else "sendPhoto"
                    field = "video" if is_video else "photo"
                    with item.open("rb") as media_file:
                        response = await client.post(
                            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}",
                            data={"chat_id": str(chat_id), "caption": title[:900]},
                            files={field: (item.name, media_file, mimetypes.guess_type(item.name)[0] or "application/octet-stream")},
                        )
                else:
                    import json
                    with ExitStack() as stack:
                        files = {}
                        entries = []
                        for index, item in enumerate(media_paths):
                            key = f"media{index}"
                            is_video = item.suffix.lower() == ".mp4"
                            entries.append({"type": "video" if is_video else "photo", "media": f"attach://{key}", **({"caption": title[:900]} if index == 0 else {})})
                            files[key] = (item.name, stack.enter_context(item.open("rb")), mimetypes.guess_type(item.name)[0] or "application/octet-stream")
                        response = await client.post(
                            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMediaGroup",
                            data={"chat_id": str(chat_id), "media": json.dumps(entries)},
                            files=files,
                        )
                response.raise_for_status()
                if not response.json().get("ok"):
                    raise ValueError("Telegram recusou o envio.")

    except Exception as error:
        print(f"Telegram send failed: {type(error).__name__}: {str(error)[:250]}")
        try:
            message = str(error)
            if "Instagram está bloqueando" in message:
                reply = "O Instagram está exigindo login ou bloqueando o acesso do servidor. Cobalt, parth-dl, gallery-dl e yt-dlp não conseguiram obter esse vídeo. Não é um problema de 45 MB."
            elif "YouTube está exigindo" in message:
                reply = "O YouTube está exigindo verificação/login do servidor. Cobalt e yt-dlp falharam; não é um problema de 45 MB."
            elif "excede 45 MB" in message:
                reply = "O vídeo ultrapassa o limite de 45 MB configurado neste bot."
            else:
                reply = "Não consegui obter esse vídeo. A fonte pode estar indisponível ou exigir autenticação."
            await _telegram_send(chat_id, reply)
        except Exception:
            pass


@app.post("/api/telegram-webhook")
async def telegram_webhook(request: Request, background_tasks: BackgroundTasks):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_WEBHOOK_SECRET:
        raise HTTPException(status_code=503, detail="Bot do Telegram não configurado.")
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != TELEGRAM_WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Webhook não autorizado.")
    data = await request.json()
    callback = data.get("callback_query")
    if callback:
        callback_id = callback.get("id")
        callback_data = callback.get("data") or ""
        callback_message = callback.get("message") or {}
        chat_id = (callback_message.get("chat") or {}).get("id")
        parts = callback_data.split(":")
        if callback_id and len(parts) == 3 and parts[0] == "dl":
            provider, choice_id = parts[1], parts[2]
            choice = telegram_choices.get(choice_id)
            if provider not in ("tikwm", "cobalt") or not choice or choice[0] != chat_id or choice[2] < time.time():
                await _telegram_answer_callback(callback_id, "Opção expirada. Envie o link novamente.")
                return {"status": "ok"}
            # A choice can only be used once; prevent duplicate downloads.
            telegram_choices.pop(choice_id, None)
            await _telegram_answer_callback(callback_id, "Preparando seu vídeo...")
            background_tasks.add_task(_telegram_process_video, chat_id, choice[1], provider)
            return {"status": "ok"}
        if callback_id:
            await _telegram_answer_callback(callback_id, "Opção inválida.")
        return {"status": "ok"}

    message = data.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    message_text = message.get("text") or message.get("caption") or ""
    if not chat_id or not isinstance(message_text, str):
        print("Telegram: update without supported text/caption", flush=True)
        return {"status": "ok"}

    if message_text.strip().startswith("/start"):
        await _telegram_send(chat_id, "Olá! Envie um link público do TikTok, Instagram, YouTube, Facebook, X, Reddit ou Pinterest. Para TikTok: TikWM ou Cobalt. Para as demais redes: Cobalt.")
        return {"status": "ok"}

    url, platform = _telegram_link_from_message(message_text)
    if not url:
        print("Telegram: link not recognized; text_length=", len(message_text), flush=True)
        await _telegram_send(chat_id, "Não reconheci o link. Envie o endereço completo de uma publicação pública (por exemplo, https://www.instagram.com/p/...).")
        return {"status": "ok"}

    print("Telegram: recognized platform=", platform, flush=True)
    await _telegram_show_choices(chat_id, url, platform)
    return {"status": "ok"}


async def _diagnose_invidious_youtube_once():
    """Temporary startup connectivity diagnostic; does not fetch video files."""
    for host in ("https://yewtu.be", "https://inv.nadeko.net", "https://invidious.nerdvpn.de"):
        try:
            async with httpx.AsyncClient(timeout=9, follow_redirects=False) as client:
                response = await client.get(host + "/api/v1/videos/YefI9Vz7ET0")
            payload = response.json() if response.status_code == 200 else {}
            formats = payload.get("formatStreams") or [] if isinstance(payload, dict) else []
            print(f"YouTube Invidious diagnostic: host={host} status={response.status_code} formats={len(formats)}", flush=True)
        except Exception as error:
            print(f"YouTube Invidious diagnostic: host={host} error={type(error).__name__}: {str(error)[:120]}", flush=True)


async def _diagnose_piped_youtube_once():
    """One-off metadata-only probe for public Piped API instances; never downloads files."""
    video_id = "YefI9Vz7ET0"
    instances = (
        "https://pipedapi.kavin.rocks",
        "https://pipedapi.adminforge.de",
        "https://api.piped.yt",
    )
    async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
        for base in instances:
            try:
                response = await client.get(f"{base}/streams/{video_id}")
                payload = response.json() if response.status_code == 200 else {}
                streams = payload.get("videoStreams") or [] if isinstance(payload, dict) else []
                playable = sum(
                    1 for stream in streams
                    if isinstance(stream, dict)
                    and stream.get("mimeType") == "video/mp4"
                    and not stream.get("videoOnly", True)
                    and isinstance(stream.get("url"), str)
                    and stream["url"].startswith("https://")
                )
                print(f"YouTube Piped diagnostic: instance={base} status={response.status_code} video_streams={len(streams)} muxed_mp4={playable}", flush=True)
            except Exception as error:
                print(f"YouTube Piped diagnostic: instance={base} error={type(error).__name__}: {str(error)[:100]}", flush=True)


@app.on_event("startup")
async def register_telegram_webhook():
    """Register webhook using Render environment secrets, without exposing them in a URL."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_WEBHOOK_SECRET:
        print("Telegram webhook: credentials not configured; skipping registration.")
        return
    asyncio.create_task(_diagnose_piped_youtube_once())
    webhook_url = os.getenv(
        "TELEGRAM_WEBHOOK_URL",
        "https://tiksave-hd-1.onrender.com/api/telegram-webhook",
    ).strip()
    if not webhook_url.startswith("https://"):
        print("Telegram webhook: invalid HTTPS URL.")
        return
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/setWebhook",
                json={
                    "url": webhook_url,
                    "secret_token": TELEGRAM_WEBHOOK_SECRET,
                    "allowed_updates": ["message", "callback_query"],
                },
            )
            response.raise_for_status()
            if response.json().get("ok"):
                print("Telegram webhook registered successfully.")
            else:
                print("Telegram webhook registration rejected by Telegram.")
    except Exception as exc:
        print(f"Telegram webhook registration failed: {type(exc).__name__}")
