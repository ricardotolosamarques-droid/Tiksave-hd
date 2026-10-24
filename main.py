
import asyncio
import ipaddress
import os
import re
import secrets
import socket
import time
from urllib.parse import urlparse

import httpx
import yt_dlp
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel


app = FastAPI(title="TikSave HD", version="3.0.0")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)

HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
}

# Tokens temporários para downloads.
# No Render, cada instância mantém sua própria memória.
DOWNLOAD_TTL = 600
download_tokens = {}

# Limite por arquivo: 250 MB.
MAX_FILE_BYTES = 250 * 1024 * 1024

# Domínios de mídia do TikTok permitidos.
MEDIA_DOMAINS = (
    "tiktok.com",
    "tiktokcdn.com",
    "tiktokcdn-us.com",
    "byteoversea.com",
    "ibytedtos.com",
    "byteimg.com",
    "muscdn.com",
    "musical.ly",
)

cache = {}
rate_limit_until = 0
RATE_LIMIT_WAIT = 60
COLLECTIONS_TTL = 300
ITEMS_TTL = 180


class VideoRequest(BaseModel):
    url: str


def valid_tiktok_url(url):
    try:
        parsed = urlparse(url.strip())
        host = (parsed.hostname or "").lower()
        return (
            parsed.scheme == "https"
            and (
                host == "tiktok.com"
                or host.endswith(".tiktok.com")
                or host == "vm.tiktok.com"
            )
        )
    except Exception:
        return False


def allowed_media_host(host):
    host = (host or "").lower().rstrip(".")
    return any(
        host == domain or host.endswith("." + domain)
        for domain in MEDIA_DOMAINS
    )


def validate_media_url(url):
    try:
        parsed = urlparse(url)
        return (
            parsed.scheme == "https"
            and parsed.username is None
            and parsed.password is None
            and parsed.port in (None, 443)
            and allowed_media_host(parsed.hostname)
        )
    except Exception:
        return False


def public_ip_addresses(host):
    try:
        addresses = socket.getaddrinfo(
            host, 443, type=socket.SOCK_STREAM
        )
        if not addresses:
            return False

        for entry in addresses:
            address = entry[4][0]
            if not ipaddress.ip_address(address).is_global:
                return False

        return True
    except Exception:
        return False


def clean_username(username):
    username = username.strip().lstrip("@")
    if not re.fullmatch(r"[A-Za-z0-9._]{1,40}", username):
        raise HTTPException(
            status_code=400,
            detail="Nome de usuário inválido."
        )
    return username


def cache_get(key):
    value = cache.get(key)
    if not value:
        return None

    expiry, payload = value
    if time.monotonic() > expiry:
        cache.pop(key, None)
        return None

    return payload


def cache_set(key, payload, ttl):
    cache[key] = (time.monotonic() + ttl, payload)


def create_download_token(url):
    if not validate_media_url(url):
        return None

    now = time.monotonic()

    for key, value in list(download_tokens.items()):
        if value["expires"] <= now:
            download_tokens.pop(key, None)

    token = secrets.token_urlsafe(32)

    download_tokens[token] = {
        "url": url,
        "expires": now + DOWNLOAD_TTL,
    }

    return token


def extract_ytdlp_formats(url):
    options = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "socket_timeout": 15,
        "retries": 2,
        "http_headers": {
            **HEADERS,
            "Referer": "https://www.tiktok.com/",
        },
    }

    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=False)

    if not info:
        return []

    formats = info.get("formats") or [info]
    output = []

    for fmt in formats:
        media_url = fmt.get("url")

        if not media_url:
            continue

        # Ignora formatos sem vídeo.
        if fmt.get("vcodec") == "none":
            continue

        # Ignora formatos sem áudio.
        if fmt.get("acodec") == "none":
            continue

        # Somente arquivos HTTP/HTTPS diretos.
        if fmt.get("protocol") not in (
            None, "https", "http"
        ):
            continue

        if not validate_media_url(media_url):
            continue

        output.append({
            "url": media_url,
            "width": int(fmt.get("width") or 0),
            "height": int(fmt.get("height") or 0),
            "bitrate_kbps": float(fmt.get("tbr") or 0),
            "source": "yt-dlp",
        })

    return output


def classify_ytdlp_error(exc):
    msg = str(exc).lower()

    if "unexpected response" in msg:
        return "resposta_inesperada_tiktok"

    if "403" in msg or "forbidden" in msg:
        return "http_403"

    if "429" in msg or "too many requests" in msg:
        return "http_429"

    return "erro_" + type(exc).__name__


async def try_ssstik(client, url):
    """
    Consulta o SSSTik sem alterar o funcionamento
    dos links diretos retornados por ele.
    """
    page = await client.get("https://ssstik.io/")
    page.raise_for_status()

    soup = BeautifulSoup(page.text, "html.parser")

    token_field = soup.find(
        "input", {"name": "tt"}
    )

    payload = {
        "id": url,
        "locale": "pt",
    }

    if token_field:
        payload["tt"] = token_field.get("value", "")

    response = await client.post(
        "https://ssstik.io/abc",
        data=payload,
        headers={
            "Referer": "https://ssstik.io/",
            "Origin": "https://ssstik.io",
            "HX-Request": "true",
        },
    )

    response.raise_for_status()

    result = BeautifulSoup(
        response.text, "html.parser"
    )

    links = []
    seen = set()

    for anchor in result.select("a[href]"):
        href = anchor.get("href", "").strip()

        if not href.startswith("https://"):
            continue

        label = anchor.get_text(
            " ", strip=True
        ) or "Sem marca d'água"

        lower = label.lower()

        if any(word in lower for word in (
            "mp3", "audio", "áudio"
        )):
            continue

        if href in seen:
            continue

        seen.add(href)

        links.append({
            "url": href,
            "label": label,
        })

    return links


@app.get("/")
def home():
    return {
        "status": "online",
        "service": "TikSave HD",
        "version": "3.0.0",
        "download_source": "yt-dlp / SSSTik",
        "file_proxy": True,
        "diagnostic_enabled": True,
    }


@app.post("/api/download")
async def download_video(data: VideoRequest):
    if not valid_tiktok_url(data.url):
        raise HTTPException(
            status_code=400,
            detail="Insira um link HTTPS válido do TikTok."
        )

    diagnostic = {
        "yt_dlp_padrao": "nao_consultado",
        "ssstik": "nao_consultado",
    }

    candidates = []

    # Primeiro: yt-dlp.
    try:
        yt_results = await asyncio.wait_for(
            asyncio.to_thread(
                extract_ytdlp_formats,
                data.url
            ),
            timeout=30,
        )

        candidates.extend(yt_results)

        diagnostic["yt_dlp_padrao"] = (
            f"{len(yt_results)}_formatos_encontrados"
        )

    except asyncio.TimeoutError:
        diagnostic["yt_dlp_padrao"] = (
            "tempo_esgotado"
        )

    except Exception as exc:
        diagnostic["yt_dlp_padrao"] = (
            classify_ytdlp_error(exc)
        )

    # Segundo: SSSTik.
    try:
        async with httpx.AsyncClient(
            timeout=25,
            follow_redirects=True,
            headers=HEADERS,
        ) as client:
            ssstik_links = await try_ssstik(
                client, data.url
            )

        diagnostic["ssstik"] = (
            f"{len(ssstik_links)}_links_encontrados"
        )

        for link in ssstik_links:
            candidates.append({
                "url": link["url"],
                "source": "ssstik",
                "label": link["label"],
                "width": 0,
                "height": 0,
                "bitrate_kbps": 0,
            })

    except httpx.HTTPStatusError as exc:
        diagnostic["ssstik"] = (
            f"http_{exc.response.status_code}"
        )

    except httpx.HTTPError as exc:
        diagnostic["ssstik"] = (
            "erro_" + type(exc).__name__
        )

    if not candidates:
        raise HTTPException(
            status_code=502,
            detail={
                "message": "Nenhum provedor retornou links.",
                "diagnostic": diagnostic,
            },
        )

    # Remove duplicados.
    unique = {}

    for item in candidates:
        if (
            item["source"] == "yt-dlp"
            and item["width"]
            and item["height"]
        ):
            key = (
                "yt-dlp",
                item["width"],
                item["height"],
                round(item["bitrate_kbps"]),
            )
        else:
            key = (
                item["source"],
                item["url"],
            )

        unique.setdefault(key, item)

    candidates = list(unique.values())

    candidates.sort(
        key=lambda item: (
            item["width"] * item["height"],
            item["bitrate_kbps"],
        ),
        reverse=True,
    )

    links = []

    for item in candidates[:12]:
        width = item["width"]
        height = item["height"]
        source = item["source"]

        quality = (
            f"{width}x{height}"
            if width and height
            else "qualidade não verificada"
        )

        label = (
            item.get("label")
            if source == "ssstik"
            else f"yt-dlp • {quality}"
        )

        # SSSTik continua usando seu link original.
        final_url = item["url"]

        if source == "yt-dlp":
            token = create_download_token(
                item["url"]
            )

            if not token:
                continue

            # Link de download pelo Render.
            final_url = f"/api/file?token={token}"

        links.append({
            "url": final_url,
            "label": label,
            "source": source,
            "width": width or None,
            "height": height or None,
            "bitrate_kbps": (
                item["bitrate_kbps"] or None
            ),
            "quality_verified": bool(
                width and height
            ),
        })

    if not links:
        raise HTTPException(
            status_code=502,
            detail="Nenhum link utilizável encontrado."
        )

    return {
        "status": "ok",
        "source": (
            "multiple"
            if len({
                x["source"] for x in links
            }) > 1
            else links[0]["source"]
        ),
        "hd_available": any(
            (x["width"] or 0) >= 720
            and (x["height"] or 0) >= 720
            for x in links
        ),
        "links": links,
        "diagnostic": diagnostic,
        "note": (
            "Links do yt-dlp usam download pelo "
            "Render. Links podem expirar."
        ),
    }


@app.get("/api/file")
async def download_file(token: str = Query(...)):
    entry = download_tokens.get(token)

    if not entry:
        raise HTTPException(
            status_code=404,
            detail="Download não encontrado."
        )

    if time.monotonic() > entry["expires"]:
        download_tokens.pop(token, None)

        raise HTTPException(
            status_code=410,
            detail="Link expirado. Gere outro download."
        )

    url = entry["url"]

    if not validate_media_url(url):
        raise HTTPException(
            status_code=400,
            detail="Origem de mídia não permitida."
        )

    parsed = urlparse(url)

    # Proteção básica contra endereços internos.
    if not await asyncio.to_thread(
        public_ip_addresses,
        parsed.hostname,
    ):
        raise HTTPException(
            status_code=403,
            detail="Endereço de mídia não permitido."
        )

    media_headers = {
        **HEADERS,
        "Referer": "https://www.tiktok.com/",
        "Accept": "*/*",
    }

    client = httpx.AsyncClient(
        timeout=httpx.Timeout(
            connect=10,
            read=30,
            write=10,
            pool=10,
        ),
        follow_redirects=False,
        trust_env=False,
        headers=media_headers,
    )

    try:
        request = client.build_request(
            "GET", url
        )

        response = await client.send(
            request, stream=True
        )

        # Não segue redirecionamentos para
        # outros endereços.
        if response.status_code in (
            301, 302, 303, 307, 308
        ):
            await response.aclose()
            await client.aclose()

            raise HTTPException(
                status_code=502,
                detail=(
                    "A CDN redirecionou o vídeo. "
                    "Gere um novo link."
                ),
            )

        if response.status_code >= 400:
            status = response.status_code
            await response.aclose()
            await client.aclose()

            raise HTTPException(
                status_code=502,
                detail=f"CDN retornou HTTP {status}.",
            )

        content_type = response.headers.get(
            "content-type", ""
        ).lower()

        if not (
            content_type.startswith("video/")
            or content_type.startswith(
                "application/octet-stream"
            )
        ):
            await response.aclose()
            await client.aclose()

            raise HTTPException(
                status_code=502,
                detail="A origem não retornou um vídeo.",
            )

        length = response.headers.get(
            "content-length"
        )

        if length and length.isdigit():
            if int(length) > MAX_FILE_BYTES:
                await response.aclose()
                await client.aclose()

                raise HTTPException(
                    status_code=413,
                    detail="Arquivo muito grande.",
                )

    except HTTPException:
        raise

    except httpx.HTTPError:
        await client.aclose()

        raise HTTPException(
            status_code=502,
            detail="Não foi possível acessar o vídeo.",
        )

    async def stream_video():
        total = 0

        try:
            async for chunk in response.aiter_bytes(
                chunk_size=65536
            ):
                total += len(chunk)

                if total > MAX_FILE_BYTES:
                    break

                yield chunk
        finally:
            await response.aclose()
            await client.aclose()

    filename = (
        f"tiksave-{int(time.time())}.mp4"
    )

    return StreamingResponse(
        stream_video(),
        media_type="video/mp4",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{filename}"'
            ),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


def normalize_collections(payload):
    """
    Mantém uma resposta JSON utilizável
    para a rota de coleções.
    """
    if isinstance(payload, dict):
        return payload

    if isinstance(payload, list):
        return {"collections": payload}

    return {"collections": []}


def normalize_items(payload):
    """
    Mantém uma resposta JSON utilizável
    para a rota de itens.
    """
    if isinstance(payload, dict):
        return payload

    if isinstance(payload, list):
        return {"items": payload}

    return {"items": []}


@app.get("/api/highlights")
async def get_highlights(
    username: str = Query(..., min_length=1),
    collection: str = Query(""),
    cursor: str = Query("0"),
):
    global rate_limit_until

    username = clean_username(username)

    if collection and not re.fullmatch(
        r"[0-9]{1,30}", collection
    ):
        raise HTTPException(
            status_code=400,
            detail="Coleção inválida."
        )

    if not re.fullmatch(
        r"[0-9]{1,30}", cursor
    ):
        raise HTTPException(
            status_code=400,
            detail="Cursor inválido."
        )

    api_url = os.getenv(
        "HIGHLIGHTS_API_URL", ""
    ).strip()

    method = os.getenv(
        "HIGHLIGHTS_API_METHOD", "GET"
    ).strip().upper()

    if (
        not api_url.startswith("https://")
        or method not in ("GET", "POST")
    ):
        raise HTTPException(
            status_code=503,
            detail=(
                "Fonte de Destaques não "
                "configurada corretamente."
            ),
        )

    cache_key = (
        username, collection, cursor
    )

    cached = cache_get(cache_key)

    if cached is not None:
        return cached

    if time.monotonic() < rate_limit_until:
        raise HTTPException(
            status_code=503,
            detail=(
                "Fonte temporariamente limitada."
            ),
        )

    try:
        async with httpx.AsyncClient(
            timeout=30,
            follow_redirects=False,
        ) as client:

            if method == "POST":
                body = (
                    {
                        "action": "posts",
                        "highlightId": collection,
                        "cursor": cursor,
                    }
                    if collection
                    else {"username": username}
                )

                response = await client.post(
                    api_url,
                    json=body,
                    headers={
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                    },
                )

            else:
                params = {
                    "username": username
                }

                if collection:
                    params.update({
                        "collection": collection,
                        "cursor": cursor,
                    })

                response = await client.get(
                    api_url,
                    params=params,
                    headers={
                        "Accept": "application/json"
                    },
                )

            if response.status_code == 429:
                retry = response.headers.get(
                    "Retry-After", ""
                )

                wait = (
                    int(retry)
                    if retry.isdigit()
                    else RATE_LIMIT_WAIT
                )

                rate_limit_until = (
                    time.monotonic()
                    + max(RATE_LIMIT_WAIT, wait)
                )

                raise HTTPException(
                    status_code=503,
                    detail=(
                        "Limite de consultas "
                        "do provedor atingido."
                    ),
                )

            response.raise_for_status()
            payload = response.json()

        result = (
            normalize_items(payload)
            if collection
            else normalize_collections(payload)
        )

        if collection:
            result.update({
                "username": username,
                "collection": collection,
            })

        cache_set(
            cache_key,
            result,
            ITEMS_TTL
            if collection
            else COLLECTIONS_TTL,
        )

        return result

    except HTTPException:
        raise

    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=502,
            detail=(
                "A fonte de Destaques retornou "
                f"HTTP {exc.response.status_code}."
            ),
        )

    except httpx.HTTPError:
        raise HTTPException(
            status_code=502,
            detail=(
                "Não foi possível consultar "
                "a fonte de Destaques."
            ),
        )

    except ValueError:
        raise HTTPException(
            status_code=502,
            detail=(
                "A fonte de Destaques "
                "não retornou JSON válido."
            ),
        )
