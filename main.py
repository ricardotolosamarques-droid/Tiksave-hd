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


# TikTok public profile browsing; opaque, expiring callbacks keep usernames out of callback_data.
telegram_profiles = {}
TELEGRAM_PROFILE_TTL = 1200


def _telegram_parse_tiktok_username(text: str):
    raw = text.strip()
    if raw.lower().startswith(("/perfil ", "@perfil ")):
        raw = raw.split(maxsplit=1)[1].strip()
    elif raw.lower() in ("/perfil", "@perfil"):
        return None
    if raw.startswith("@"):
        raw = raw[1:]
    if raw.startswith("https://"):
        parsed = urlparse(raw)
        if (parsed.hostname or "").lower() not in ("www.tiktok.com", "m.tiktok.com", "tiktok.com"):
            return None
        match = re.fullmatch(r"/@([A-Za-z0-9._]{2,24})/?", parsed.path)
        if not match:
            return None
        raw = match.group(1)
    return raw if re.fullmatch(r"[A-Za-z0-9._]{2,24}", raw) else None


def _telegram_profile_ytdlp(username: str, cursor: int = 0):
    """Fallback: list public profile entries without downloading media."""
    url = f"https://www.tiktok.com/@{username}"
    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True,
                          "extract_flat": "in_playlist", "playlistend": min(cursor + 10, 50),
                          "socket_timeout": 12, "retries": 0, "ignoreerrors": True}) as ydl:
        result = ydl.extract_info(url, download=False)
    entries = (result or {}).get("entries") or []
    videos = []
    for item in list(entries)[cursor:cursor + 10]:
        if not isinstance(item, dict):
            continue
        video_id = str(item.get("id") or "")
        if re.fullmatch(r"[0-9]{10,25}", video_id):
            videos.append({"id": video_id, "title": item.get("title") or item.get("description") or "Sem descrição"})
    return {"videos": videos, "cursor": cursor + len(videos), "hasMore": len(videos) == 10 and cursor + 10 < 50}


async def _telegram_profile_tikhub(username: str, cursor: int = 0):
    """Optional paid API; configured only via Render secret TIKHUB_API_KEY."""
    key = os.getenv("TIKHUB_API_KEY", "").strip()
    if not key:
        raise RuntimeError("TIKHUB_API_KEY not configured")
    async with httpx.AsyncClient(timeout=25) as client:
        response = await client.get(
            "https://api.tikhub.io/api/v1/tiktok/app/v3/fetch_user_post_videos_v3",
            params={"unique_id": username, "max_cursor": cursor, "count": 10},
            headers={"Authorization": f"Bearer {key}"},
        )
        response.raise_for_status()
        payload = response.json()
    if payload.get("code") not in (0, 200):
        raise ValueError(f"TikHub API response code {payload.get('code')}")
    data = payload.get("data") or {}
    if not isinstance(data, dict):
        raise ValueError("Unexpected TikHub data format")
    raw = data.get("aweme_list") or data.get("itemList") or data.get("videos") or []
    videos = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        video_id = str(item.get("aweme_id") or item.get("id") or item.get("video_id") or "")
        if re.fullmatch(r"[0-9]{10,25}", video_id):
            videos.append({"id": video_id, "title": item.get("desc") or item.get("title") or "Sem descrição"})
    return {"videos": videos, "cursor": data.get("max_cursor") or data.get("cursor") or 0,
            "hasMore": data.get("has_more") or data.get("hasMore")}


def _telegram_profile_ttdlp(username: str, cursor: int = 0):
    """Scan public profile using tt-dlp dry-run, never downloading media."""
    import subprocess
    import sys
    with tempfile.TemporaryDirectory(prefix="ttdlp_profile_") as folder:
        command = [sys.executable, "-m", "tt_dlp", "--dry-run", "--no-stories",
                   "--limit", "40", "--output", folder,
                   "--profile-store", os.path.join(folder, "profiles.json"),
                   f"@{username}"]
        result = subprocess.run(command, capture_output=True, text=True,
                                timeout=65, cwd=folder, check=False)
    print(f"TikTok tt-dlp exit={result.returncode}; output={result.stdout[-450:]!r}; error={result.stderr[-250:]!r}", flush=True)
    if result.returncode:
        raise RuntimeError("tt-dlp profile scan failed")
    items = []
    seen = set()
    for line in result.stdout.splitlines():
        if not line.startswith("[dry-run] "):
            continue
        filename = line[len("[dry-run] "):]
        match = re.match(r"^([0-9]{10,25})(?:[_ ]|[.])", filename)
        if not match or match.group(1) in seen:
            continue
        seen.add(match.group(1))
        items.append({"id": match.group(1), "title": filename[len(match.group(1)):].strip(" _")[:90]})
    return {"videos": items[:40], "cursor": len(items), "hasMore": False}


async def _telegram_show_tiktok_profile(chat_id: int, username: str, cursor: int = 0):
    try:
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
                response = await client.get(
                    "https://www.tikwm.com/api/user/posts",
                    params={"unique_id": username, "count": 40, "cursor": 0},
                )
                response.raise_for_status()
                payload = response.json()
            data = payload.get("data") or {}
            videos = data.get("videos") or []
            if payload.get("code") != 0 or not isinstance(videos, list):
                raise ValueError("TikWM returned invalid profile data")
        except Exception as tikwm_exc:
            print(f"TikTok profile TikWM unavailable: {type(tikwm_exc).__name__}", flush=True)
            if os.getenv("TIKHUB_API_KEY", "").strip():
                try:
                    data = await _telegram_profile_tikhub(username, cursor)
                    videos = data["videos"]
                except Exception as api_exc:
                    print(f"TikTok profile TikHub unavailable: {type(api_exc).__name__}: {str(api_exc)[:100]}", flush=True)
                    data = await asyncio.wait_for(
                        asyncio.to_thread(_telegram_profile_ytdlp, username, cursor), timeout=45
                    )
                    videos = data["videos"]
            else:
                try:
                    data = await asyncio.wait_for(
                        asyncio.to_thread(_telegram_profile_ttdlp, username, cursor), timeout=75
                    )
                    videos = data["videos"]
                except Exception as ttdlp_exc:
                    print(f"TikTok profile tt-dlp unavailable: {type(ttdlp_exc).__name__}: {str(ttdlp_exc)[:100]}", flush=True)
                    data = await asyncio.wait_for(
                        asyncio.to_thread(_telegram_profile_ytdlp, username, cursor), timeout=45
                    )
                    videos = data["videos"]
        if not videos:
            await _telegram_send(chat_id, "Não consegui listar publicações desse perfil pelas fontes disponíveis.")
            return
        urls = []
        for video in videos[:40]:
            if not isinstance(video, dict):
                continue
            video_id = str(video.get("video_id") or video.get("id") or "")
            if not re.fullmatch(r"[0-9]{10,25}", video_id):
                continue
            urls.append(f"https://www.tiktok.com/@{username}/video/{video_id}")
        if not urls:
            await _telegram_send(chat_id, "Não encontrei vídeos públicos válidos nesse perfil.")
            return
        await _telegram_send(
            chat_id,
            f"Encontrei {len(urls)} publicações de @{username}. Vou enviar os vídeos automaticamente, um por vez. Alguns podem falhar ou ser fotos."
        )
        for index, url in enumerate(urls, 1):
            try:
                await _telegram_process_video(chat_id, url, "tikwm")
            except Exception as error:
                print(f"Telegram profile auto-send item {index} failed: {type(error).__name__}", flush=True)
        await _telegram_send(chat_id, f"Envio do perfil @{username} finalizado. Foram processadas {len(urls)} publicações.")
    except Exception as exc:
        print(f"TikTok profile lookup failed: {type(exc).__name__}: {str(exc)[:150]}", flush=True)
        await _telegram_send(chat_id, "Não foi possível carregar os posts públicos desse perfil agora.")


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
        "js_runtimes": {"node": {}},
        "remote_components": ["ejs:github"],
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
                "js_runtimes": {"node": {}},
                "remote_components": ["ejs:github"],
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


def _telegram_download_tiktok_photos(url: str, folder: str):
    """Download up to ten public photo slides through TikWM."""
    import ipaddress
    import socket
    from urllib.parse import urljoin
    with httpx.Client(timeout=30, follow_redirects=False) as client:
        response = client.get("https://www.tikwm.com/api/", params={"url": url})
        response.raise_for_status()
        data = (response.json().get("data") or {})
        images = data.get("images") or []
        if not isinstance(images, list) or not images:
            raise ValueError("Fotos não disponíveis nesta fonte.")
        paths = []
        for index, item in enumerate(images[:10]):
            media_url = item if isinstance(item, str) else (item.get("url") or "") if isinstance(item, dict) else ""
            media_url = urljoin("https://www.tikwm.com/", media_url)
            for redirect in range(4):
                parsed = urlparse(media_url)
                host = (parsed.hostname or "").lower()
                if parsed.scheme != "https" or parsed.username or parsed.password or not host or not (host == "tikwm.com" or host.endswith((".tikwm.com", ".tiktokcdn.com", ".tiktokcdn-us.com", ".byteoversea.com", ".byteoversea.net", ".ibytedtos.com"))):
                    raise ValueError("Domínio de imagem não permitido.")
                addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
                if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
                    raise ValueError("Destino de imagem não público.")
                with client.stream("GET", media_url) as media:
                    if media.status_code in (301, 302, 303, 307, 308):
                        media_url = urljoin(media_url, media.headers.get("location") or "")
                        continue
                    media.raise_for_status()
                    output = Path(folder) / f"photo_{index}.jpg"
                    total = 0
                    with output.open("wb") as f:
                        for chunk in media.iter_bytes(65536):
                            total += len(chunk)
                            if total > 10 * 1024 * 1024:
                                raise ValueError("Foto maior que 10 MB.")
                            f.write(chunk)
                    with output.open("rb") as f:
                        if f.read(2) != bytes([255, 216]):
                            raise ValueError("Imagem não é JPEG.")
                    paths.append(output)
                    break
            else:
                raise ValueError("Redirecionamentos excessivos.")
    return data.get("title") or "Fotos do TikTok", paths


def _telegram_download_tiktok_ttdlp(url: str, folder: str):
    """Fallback for public TikTok videos and image carousels."""
    import subprocess
    import sys
    output = Path(folder) / "ttdlp_media"
    output.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, "-m", "tt_dlp", "--output", str(output), "--no-stories", url]
    result = subprocess.run(command, capture_output=True, text=True, timeout=100, check=False, cwd=str(output))
    files = sorted((p for p in output.rglob("*") if p.is_file() and p.suffix.lower() in (".mp4", ".jpg", ".jpeg", ".png")), key=lambda p: str(p))
    if not files:
        print(f"TikTok tt-dlp media failed: exit={result.returncode}; {(result.stderr or result.stdout)[-300:]}", flush=True)
        raise ValueError("tt-dlp não encontrou mídia pública.")
    files = files[:10]
    for item in files:
        if item.stat().st_size < 1024 or item.stat().st_size > TELEGRAM_MAX_VIDEO_BYTES:
            raise ValueError("Mídia vazia ou maior que o limite.")
    return "Publicação do TikTok", files if len(files) > 1 else files[0]


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
                if provider == "tikwm" and host in ("tiktok.com", "www.tiktok.com", "m.tiktok.com"):
                    if "Publicação de fotos" in str(cobalt_error):
                        try:
                            title, path = await asyncio.to_thread(_telegram_download_tiktok_photos, url, folder)
                        except Exception as photo_error:
                            print(f"Telegram TikTok photos TikWM failed: {type(photo_error).__name__}: {str(photo_error)[:160]}", flush=True)
                            title, path = await asyncio.to_thread(_telegram_download_tiktok_ttdlp, url, folder)
                    else:
                        try:
                            title, path = await asyncio.to_thread(_telegram_download_yt_dlp, url, folder)
                        except Exception as yt_error:
                            print(f"Telegram TikTok yt-dlp fallback failed: {type(yt_error).__name__}", flush=True)
                            try:
                                title, path = await asyncio.to_thread(_telegram_download_cobalt, url, folder)
                            except Exception as cobalt_fallback_error:
                                print(f"Telegram TikTok Cobalt fallback failed: {type(cobalt_fallback_error).__name__}: {str(cobalt_fallback_error)[:160]}", flush=True)
                                title, path = await asyncio.to_thread(_telegram_download_tiktok_ttdlp, url, folder)
                elif provider != "cobalt" or not (host == "instagram.com" or host.endswith(".instagram.com") or host in ("youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be")):
                    raise
                else:
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


def _telegram_instagram_stories_via_saveapi(username: str, folder: str):
    """Optional third-party provider. Configure SAVEAPI_KEY in Render secrets."""
    key = os.getenv("SAVEAPI_KEY", "").strip()
    if not key:
        return None
    destination = Path(folder) / "saveapi_stories"
    destination.mkdir(parents=True, exist_ok=True)
    with httpx.Client(timeout=55, follow_redirects=True) as client:
        response = client.get("https://api.saveapi.org/v1/instagram",
                              headers={"Authorization": f"Bearer {key}"},
                              params={"url": f"https://www.instagram.com/stories/{username}/"})
        if response.status_code in (401, 403):
            raise ValueError("Chave SaveAPI inválida ou sem permissão.")
        if response.status_code == 429:
            raise ValueError("Limite de consultas SaveAPI atingido.")
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Resposta inválida da SaveAPI.")
        if payload.get("error") or payload.get("code") == "MEDIA_NOT_FOUND":
            raise ValueError("SaveAPI não encontrou Stories públicos disponíveis.")
        data = payload.get("data")
        medias = payload.get("medias") or (data.get("medias") if isinstance(data, dict) else None) or []
        if not isinstance(medias, list):
            raise ValueError("Lista de Stories inválida.")
        saved = []
        for i, item in enumerate(medias[:30]):
            if not isinstance(item, dict):
                continue
            url = item.get("url")
            if not isinstance(url, str) or not url.startswith("https://"):
                continue
            ext = ".mp4" if "video" in str(item.get("type", "")).lower() else ".jpg"
            target = destination / f"story_{i:02d}{ext}"
            try:
                with client.stream("GET", url) as stream:
                    stream.raise_for_status()
                    with target.open("wb") as handle:
                        total = 0
                        for chunk in stream.iter_bytes(chunk_size=65536):
                            total += len(chunk)
                            if total > TELEGRAM_MAX_VIDEO_BYTES:
                                raise ValueError("Arquivo grande demais.")
                            handle.write(chunk)
                if 1024 <= target.stat().st_size <= TELEGRAM_MAX_VIDEO_BYTES:
                    saved.append(target)
            except Exception as exc:
                print(f"SaveAPI story {i} download failed: {type(exc).__name__}", flush=True)
                target.unlink(missing_ok=True)
        return saved


def _telegram_instagram_stories_instaloader(username: str, folder: str):
    """Optional Instaloader session-file integration; never handles Instagram passwords."""
    session_file = os.getenv("INSTAGRAM_SESSION_FILE", "").strip()
    session_user = os.getenv("INSTAGRAM_SESSION_USER", "").strip()
    if not session_file or not session_user:
        return None
    if not Path(session_file).is_file():
        raise ValueError("Arquivo de sessão do Instagram não encontrado no servidor.")
    import instaloader
    destination = Path(folder) / "instaloader"
    destination.mkdir(parents=True, exist_ok=True)
    loader = instaloader.Instaloader(
        dirname_pattern=str(destination / "{target}"),
        filename_pattern="{date_utc}_UTC",
        download_videos=True,
        download_video_thumbnails=False,
        save_metadata=False,
        post_metadata_txt_pattern="",
        quiet=True,
    )
    try:
        loader.load_session_from_file(session_user, filename=session_file)
        profile = instaloader.Profile.from_username(loader.context, username)
        for story in loader.get_stories(userids=[profile.userid]):
            for item in story.get_items():
                loader.download_storyitem(item, target=username)
    except instaloader.exceptions.LoginRequiredException as exc:
        raise ValueError("A sessão do Instagram expirou ou não tem permissão para consultar Stories.") from exc
    except instaloader.exceptions.ConnectionException as exc:
        raise ValueError("O Instagram recusou ou limitou a consulta autenticada.") from exc
    media = sorted((p for p in destination.rglob("*") if p.is_file() and p.suffix.lower() in (".mp4", ".jpg", ".jpeg", ".png")))
    return [p for p in media if 1024 <= p.stat().st_size <= TELEGRAM_MAX_VIDEO_BYTES][:30]


def _telegram_instagram_stories_fetch(username: str, folder: str):
    """Try the dedicated Stories extractor, then the profile extractor, without bypassing login."""
    import subprocess
    import sys
    authenticated = _telegram_instagram_stories_instaloader(username, folder)
    if authenticated is not None:
        if authenticated:
            return authenticated
        raise ValueError("Não foram encontrados Stories acessíveis pela sessão autorizada.")
    external = _telegram_instagram_stories_via_saveapi(username, folder)
    if external is not None:
        if external:
            return external
        raise ValueError("SaveAPI não retornou Stories disponíveis.")
    destination = Path(folder) / "stories"
    destination.mkdir(parents=True, exist_ok=True)
    attempts = [
        ("stories", f"https://www.instagram.com/stories/{username}/"),
        ("profile", f"https://www.instagram.com/{username}/"),
    ]
    diagnostics = []
    for kind, url in attempts:
        cmd = [sys.executable, "-m", "gallery_dl", "--directory", str(destination),
               "--range", "1-30", "--retries", "0"]
        if kind == "profile":
            cmd += ["--option", "extractor.instagram.include=stories"]
        cmd.append(url)
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=90, check=False)
        except subprocess.TimeoutExpired:
            diagnostics.append(f"{kind}: timeout")
            continue
        media = sorted((p for p in destination.rglob("*") if p.is_file() and p.suffix.lower() in (".mp4", ".jpg", ".jpeg", ".png")))
        media = [p for p in media if 1024 <= p.stat().st_size <= TELEGRAM_MAX_VIDEO_BYTES]
        if media:
            return media[:30]
        diagnostic = (proc.stderr or proc.stdout or "")[-350:]
        print(f"Instagram stories @{username} ({kind}): gallery-dl exit={proc.returncode} {diagnostic}", flush=True)
        diagnostics.append(diagnostic)
        lower = diagnostic.lower()
        if any(v in lower for v in ("authrequired", "login required", "authentication required", "cookies needed", "401 unauthorized", "429 too many requests")):
            raise ValueError("O Instagram exige autenticação ou limitou o acesso; não é possível obter esses Stories sem permissão.")
    combined = " ".join(diagnostics).lower()
    if "notfounderror" in combined or "keyerror" in combined:
        raise ValueError("O Instagram não disponibilizou os dados dos Stories aos extratores; isso não significa que o perfil não exista.")
    raise ValueError("Não foram encontrados Stories acessíveis nessa consulta.")


async def _telegram_show_instagram_stories(chat_id: int, username: str):
    import tempfile
    try:
        with tempfile.TemporaryDirectory(prefix="ig_stories_") as folder:
            media = await asyncio.to_thread(_telegram_instagram_stories_fetch, username, folder)
            if not media:
                await _telegram_send(chat_id, "Não encontrei Stories acessíveis nesse perfil.")
                return
            await _telegram_send(chat_id, f"Encontrei {len(media)} arquivo(s) de Stories de @{username}. Enviando...")
            import mimetypes
            for path in media:
                try:
                    method = "sendVideo" if path.suffix.lower() == ".mp4" else "sendPhoto"
                    field = "video" if method == "sendVideo" else "photo"
                    async with httpx.AsyncClient(timeout=90) as client:
                        with path.open("rb") as handle:
                            response = await client.post(
                                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}",
                                data={"chat_id": str(chat_id)},
                                files={field: (path.name, handle, mimetypes.guess_type(path.name)[0] or "application/octet-stream")},
                            )
                        response.raise_for_status()
                except Exception as exc:
                    print(f"Instagram story send failed: {type(exc).__name__}: {str(exc)[:140]}", flush=True)
            await _telegram_send(chat_id, f"Consulta de Stories de @{username} concluída.")
    except Exception as exc:
        print(f"Instagram stories lookup @{username} failed: {type(exc).__name__}: {str(exc)[:150]}", flush=True)
        await _telegram_send(chat_id, f"Não consegui recuperar os Stories de @{username}. Motivo: {str(exc)[:240]}")


# Authorization is per Telegram user and expires after 24 hours.
# In-memory grants reset when Render restarts or redeploys.
telegram_stories_grants = {}


async def _telegram_stories_delete_code_message(chat_id: int, message_id: int):
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            await client.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/deleteMessage",
                json={"chat_id": chat_id, "message_id": message_id},
            )
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
        if callback_id and len(parts) == 3 and parts[0] == "tp":
            item = telegram_profiles.pop(parts[2], None)
            if not item or item[0] != chat_id or item[3] < time.time():
                await _telegram_answer_callback(callback_id, "Pesquisa expirada. Envie /perfil @usuario novamente.")
            elif parts[1] == "v":
                await _telegram_answer_callback(callback_id, "Escolha como baixar:")
                await _telegram_show_choices(chat_id, item[2], "tiktok")
            elif parts[1] == "n":
                await _telegram_answer_callback(callback_id, "Carregando mais posts...")
                background_tasks.add_task(_telegram_show_tiktok_profile, chat_id, item[1], item[2])
            else:
                await _telegram_answer_callback(callback_id, "Opção inválida.")
            return {"status": "ok"}
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

    # The access code is only accepted in a private chat; never log it.
    command = message_text.strip().split(maxsplit=1)[0].split("@")[0].lower() if message_text.strip() else ""
    sender_id = (message.get("from") or {}).get("id")
    chat_type = (message.get("chat") or {}).get("type")
    stories_access_code = os.getenv("TELEGRAM_STORIES_ACCESS_CODE", "").strip()
    if command == "/ativar":
        if chat_type != "private" or not sender_id:
            await _telegram_send(chat_id, "Por segurança, envie /ativar somente na conversa privada com o bot.")
            return {"status": "ok"}
        if not stories_access_code:
            await _telegram_send(chat_id, "O administrador ainda não configurou o código de acesso aos Stories.")
            return {"status": "ok"}
        supplied = message_text.strip().partition(" ")[2].strip()
        if not supplied:
            await _telegram_send(chat_id, "Envie /ativar SEU_CODIGO em conversa privada. A autorização dura 24 horas.")
            return {"status": "ok"}
        # Remove the message containing the code where Telegram permissions allow it.
        if message.get("message_id"):
            await _telegram_stories_delete_code_message(chat_id, message["message_id"])
        import hmac
        if hmac.compare_digest(supplied.encode(), stories_access_code.encode()):
            telegram_stories_grants[int(sender_id)] = time.time() + 86400
            await _telegram_send(chat_id, "Acesso aos Stories liberado por 24 horas! Use /stories @usuario.")
        else:
            await _telegram_send(chat_id, "Código inválido. Confira e tente novamente em conversa privada.")
        return {"status": "ok"}

    if command == "/stories":
        if not stories_access_code:
            await _telegram_send(chat_id, "O acesso aos Stories ainda não foi configurado pelo administrador.")
            return {"status": "ok"}
        if not sender_id or telegram_stories_grants.get(int(sender_id), 0) <= time.time():
            await _telegram_send(chat_id, "Acesso protegido. Abra a conversa privada com o bot e envie /ativar SEU_CODIGO. A liberação vale por 24 horas.")
            return {"status": "ok"}
        raw = message_text.strip().split(maxsplit=1)
        username = raw[1].strip().lstrip("@") if len(raw) == 2 else ""
        if not re.fullmatch(r"[A-Za-z0-9._]{1,30}", username):
            await _telegram_send(chat_id, "Use /stories @usuario (perfil público do Instagram).")
        else:
            await _telegram_send(chat_id, f"Consultando Stories disponíveis de @{username}...")
            background_tasks.add_task(_telegram_show_instagram_stories, chat_id, username)
        return {"status": "ok"}

    if message_text.strip().startswith("/perfil") or message_text.strip().startswith("@"):
        username = _telegram_parse_tiktok_username(message_text)
        if not username:
            await _telegram_send(chat_id, "Use /perfil @usuario (exemplo: /perfil @tiktok).")
        else:
            background_tasks.add_task(_telegram_show_tiktok_profile, chat_id, username)
        return {"status": "ok"}

    if message_text.strip().startswith("/start"):
        await _telegram_send(chat_id, "Olá! Envie um link público do TikTok, Instagram, YouTube, Facebook, X, Reddit ou Pinterest. Para TikTok: TikWM ou Cobalt. Para as demais redes: Cobalt. Para ver posts públicos de um perfil TikTok, envie /perfil @usuario.")
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


async def _diagnose_ytdlp_runtime_once():
    """Inspect available JS runtimes and yt-dlp version without downloading media."""
    import shutil
    import subprocess
    import sys
    for name in ("deno", "node", "bun", "qjs"):
        executable = shutil.which(name)
        if not executable:
            print(f"YouTube runtime diagnostic: {name}=not_found", flush=True)
            continue
        try:
            result = await asyncio.to_thread(
                subprocess.run, [executable, "--version"],
                capture_output=True, text=True, timeout=8, check=False
            )
            version = (result.stdout or result.stderr).splitlines()
            print(f"YouTube runtime diagnostic: {name}=present version={(version[0] if version else 'unknown')[:90]}", flush=True)
        except Exception as error:
            print(f"YouTube runtime diagnostic: {name}=present error={type(error).__name__}", flush=True)
    print(f"YouTube runtime diagnostic: yt_dlp={yt_dlp.version.__version__} python={sys.version.split()[0]}", flush=True)


async def _diagnose_youtube_node_once():
    """Metadata-only YouTube extraction probe using the installed Node.js runtime."""
    def probe():
        options = {
            "quiet": True, "no_warnings": False, "skip_download": True,
            "noplaylist": True, "socket_timeout": 15, "retries": 0,
            "js_runtimes": {"node": {}},
            "remote_components": ["ejs:github"],
        }
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info("https://www.youtube.com/watch?v=YefI9Vz7ET0", download=False)
            return len((info or {}).get("formats") or [])
    try:
        formats = await asyncio.wait_for(asyncio.to_thread(probe), timeout=65)
        print(f"YouTube Node diagnostic: success formats={formats}", flush=True)
    except Exception as error:
        print(f"YouTube Node diagnostic: failed {type(error).__name__}: {str(error)[:240]}", flush=True)


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
    # Run the metadata-only YouTube probe on every test region, even without Telegram credentials.
    asyncio.create_task(_diagnose_youtube_node_once())
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_WEBHOOK_SECRET:
        print("Telegram webhook: credentials not configured; skipping registration.")
        return
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
