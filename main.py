
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

rate_limit_until = 0


class VideoRequest(BaseModel):
    url: str


def valid_tiktok_url(value):
    try:
        p = urlparse(value)
        host = (p.hostname or "").lower()
        return (
            p.scheme == "https"
            and (
                host == "tiktok.com"
                or host.endswith(".tiktok.com")
            )
            and not p.username
            and not p.password
        )
    except (ValueError, TypeError):
        return False


def safe_media_url(value):
    if not isinstance(value, str):
        return None

    try:
        p = urlparse(value)

        if (
            p.scheme == "https"
            and p.hostname
            and not p.username
            and not p.password
        ):
            return value

    except (ValueError, TypeError):
        pass

    return None


def first_url(value):
    if isinstance(value, str):
        return safe_media_url(value)

    if isinstance(value, list):
        return next(
            (v for x in value if (v := first_url(x))),
            None,
        )

    if isinstance(value, dict):
        return next(
            (
                v
                for k in ("url_list", "url", "uri")
                if (v := first_url(value.get(k)))
            ),
            None,
        )

    return None


def extract_media(story):
    if not isinstance(story, dict):
        return []

    results = []
    video = story.get("video") or {}

    if isinstance(video, dict):
        url = (
            first_url(video.get("play_addr"))
            or first_url(video.get("download_addr"))
            or first_url(video.get("url"))
        )

        if url:
            results.append({
                "type": "video",
                "url": url,
                "downloadAllowed": True,
            })

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
            url = (
                first_url(item.get("display_image"))
                or first_url(item.get("owner_watermark_image"))
                or first_url(item.get("url_list"))
                or first_url(item.get("url"))
            )
        else:
            url = first_url(item)

        if url and url not in images:
            images.append(url)

    if images:
        results.append({
            "type": "image",
            "images": images,
            "downloadAllowed": True,
        })

    return results


def clean_username(value):
    value = value.strip()

    if "tiktok.com/@" in value:
        value = (
            value.split("tiktok.com/@", 1)[1]
            .split("?", 1)[0]
            .split("/", 1)[0]
        )

    value = value.lstrip("@")

    if not re.fullmatch(r"[A-Za-z0-9._]{2,24}", value):
        raise HTTPException(
            status_code=400,
            detail="Nome de usuário inválido.",
        )

    return value


def cache_get(key):
    entry = cache.get(key)

    if not entry:
        return None

    expires, value = entry

    if time.monotonic() >= expires:
        cache.pop(key, None)
        return None

    return value


def cache_set(key, value, ttl):
    cache[key] = (time.monotonic() + ttl, value)


def extract_ssstik_token(html):
    patterns = [
        r"""s_tt\s*=\s*['"]([^'"]+)['"]""",
        r"""["']tt["']\s*:\s*["']([^"']+)["']""",
        r"""name=["']tt["'][^>]*value=["']([^'"]+)["']""",
        r"""value=["']([^'"]+)["'][^>]*name=["']tt["']""",
    ]

    for pattern in patterns:
        m = re.search(pattern, html, re.I)

        if m:
            return m.group(1)

    return ""


def extract_ssstik_links(html):
    soup = BeautifulSoup(html, "html.parser")
    links = []
    seen = set()

    selector = (
        "a[href], a[data-href], a[data-url], "
        "a[data-download], button[data-href], "
        "button[data-url]"
    )

    for el in soup.select(selector):
        raw = next(
            (
                v.strip()
                for attr in (
                    "href",
                    "data-href",
                    "data-url",
                    "data-download",
                )
                if isinstance(
                    (v := el.get(attr)), str
                ) and v.strip()
            ),
            "",
        )

        if not raw:
            continue

        url = safe_media_url(
            urljoin(SSSTIK_HOME, raw)
        )

        if not url or url in seen:
            continue

        label = (
            el.get_text(" ", strip=True)
            or str(
                el.get("aria-label")
                or el.get("title")
                or "Baixar vídeo"
            )
        )

        description = " ".join((
            label,
            str(el.get("title", "")),
            str(el.get("aria-label", "")),
            str(el.get("class", "")),
        )).lower()

        if any(
            word in description
            for word in (
                "mp3",
                "audio",
                "áudio",
                "music",
                "somente som",
                "sound",
            )
        ):
            continue

        if any(
            word in urlparse(url).path.lower()
            for word in (
                ".mp3",
                "/mp3",
                "/audio",
            )
        ):
            continue

        if not any(
            word in description
            for word in (
                "download",
                "baixar",
                "watermark",
                "marca",
                "hd",
                "1080",
                "quality",
                "video",
                "vídeo",
            )
        ):
            continue

        if (
            urlparse(url).hostname == "ssstik.io"
            and urlparse(url).path
            in ("/", "/abc", "/download")
        ):
            continue

        seen.add(url)

        links.append({
            "url": url,
            "label": label,
        })

    return links


async def try_ssstik(client, video_url):
    page = await client.get(SSSTIK_HOME)
    page.raise_for_status()

    response = await client.post(
        SSSTIK_ENDPOINT,
        data={
            "id": video_url,
            "locale": "pt",
            "tt": extract_ssstik_token(page.text),
        },
        headers={
            "Accept": "text/html, */*",
            "HX-Request": "true",
            "HX-Target": "target",
            "HX-Trigger": "_gcaptcha_pt",
            "Origin": "https://ssstik.io",
            "Referer": SSSTIK_HOME,
        },
    )

    response.raise_for_status()

    return extract_ssstik_links(response.text)


def extract_ytdlp_formats(video_url):
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "socket_timeout": 12,
        "retries": 1,
        "extractor_retries": 1,
        "cachedir": False,
    }

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(
            video_url,
            download=False,
        )

    if not isinstance(info, dict):
        return []

    formats = info.get("formats") or [info]
    output = []

    for fmt in formats:
        url = safe_media_url(fmt.get("url"))

        if not url:
            continue

        if fmt.get("vcodec") in ("none", None):
            continue

        if fmt.get("acodec") == "none":
            continue

        if fmt.get("protocol") not in (
            None,
            "https",
            "http",
        ):
            continue

        output.append({
            "url": url,
            "width": int(fmt.get("width") or 0),
            "height": int(fmt.get("height") or 0),
            "bitrate_kbps": float(
                fmt.get("tbr") or 0
            ),
            "source": "yt-dlp",
        })

    return output


def classify_ytdlp_error(exc):
    msg = str(exc).lower()

    if "unexpected response from webpage request" in msg:
        return "resposta_inesperada_tiktok"

    if "403" in msg or "forbidden" in msg:
        return "http_403"

    if "429" in msg or "too many requests" in msg:
        return "http_429"

    return "erro_" + type(exc).__name__


@app.get("/")
def home():
    return {
        "status": "online",
        "service": "TikSave HD",
        "version": "3.1.0",
        "file_proxy": True,
        "download_source": "yt-dlp / SSSTik",
        "diagnostic_enabled": True,
    }


@app.post("/api/download")
async def download_video(data: VideoRequest):
    if not valid_tiktok_url(data.url):
        raise HTTPException(
            status_code=400,
            detail=(
                "Insira um link HTTPS válido "
                "do TikTok."
            ),
        )

    diagnostic = {
        "yt_dlp_padrao": "nao_consultado",
        "ssstik": "nao_consultado",
    }

    candidates = []

    try:
        candidates = await asyncio.wait_for(
            asyncio.to_thread(
                extract_ytdlp_formats,
                data.url,
            ),
            timeout=30,
        )

        diagnostic["yt_dlp_padrao"] = (
            f"{len(candidates)}_formatos_encontrados"
        )

    except asyncio.TimeoutError:
        diagnostic["yt_dlp_padrao"] = (
            "tempo_esgotado"
        )

    except Exception as exc:
        diagnostic["yt_dlp_padrao"] = (
            classify_ytdlp_error(exc)
        )

    headers = {
        "User-Agent": (
            "Mozilla/5.0 "
            "(Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 "
            "Chrome/130.0.0.0 Safari/537.36"
        ),
        "Accept-Language": (
            "pt-BR,pt;q=0.9,en;q=0.8"
        ),
    }

    try:
        async with httpx.AsyncClient(
            timeout=25,
            follow_redirects=True,
            headers=headers,
        ) as client:
            ssstik_links = await try_ssstik(
                client,
                data.url,
            )

        diagnostic["ssstik"] = (
            f"{len(ssstik_links)}_links_encontrados"
        )

        for link in ssstik_links:
            description = (
                link["label"]
                + " "
                + urlparse(link["url"]).path
            ).lower()

            if any(
                x in description
                for x in (
                    "mp3",
                    "audio",
                    "áudio",
                    "/music",
                )
            ):
                continue

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
                "message": (
                    "Nenhum provedor retornou links."
                ),
                "diagnostic": diagnostic,
            },
        )

    unique = {}

    for c in candidates:
        if (
            c["source"] == "yt-dlp"
            and c["width"]
            and c["height"]
        ):
            key = (
                "yt-dlp",
                c["width"],
                c["height"],
                round(c["bitrate_kbps"]),
            )
        else:
            key = (
                c["source"],
                c["url"],
            )

        unique.setdefault(key, c)

    candidates = list(unique.values())

    candidates.sort(
        key=lambda c: (
            c["width"] * c["height"],
            c["bitrate_kbps"],
        ),
        reverse=True,
    )

    links = []
    preview_url = None

    for c in candidates[:12]:
        width = c["width"]
        height = c["height"]

        quality = (
            f"{width}x{height}"
            if width and height
            else "qualidade não verificada"
        )

        label = f"{c['source']} • {quality}"
        download_url = c["url"]

        if c["source"] == "yt-dlp":
            token = create_download_token(
                c["url"]
            )

            if not token:
                continue

            download_url = (
                "/api/file?token=" + token
            )

            if preview_url is None:
                preview_url = c["url"]

        links.append({
            "url": download_url,
            "label": label,
            "source": c["source"],
            "width": width or None,
            "height": height or None,
            "bitrate_kbps": (
                c["bitrate_kbps"] or None
            ),
            "quality_verified": bool(
                width and height
            ),
        })

    return {
        "status": "ok",
        "source": (
            "multiple"
            if len(set(
                c["source"]
                for c in candidates
            )) > 1
            else candidates[0]["source"]
        ),
        "hd_available": any(
            c["width"] >= 720
            and c["height"] >= 720
            for c in candidates
        ),
        "links": links,
        "preview_url": preview_url,
        "diagnostic": diagnostic,
        "note": (
            "Qualidade não verificada não "
            "significa qualidade baixa. "
            "Links podem expirar."
        ),
    }


def allowed_media_url(value):
    try:
        parsed = urlparse(value)
        host = (
            parsed.hostname or ""
        ).lower().rstrip(".")

        return (
            parsed.scheme == "https"
            and parsed.port in (None, 443)
            and parsed.username is None
            and parsed.password is None
            and any(
                host == d
                or host.endswith("." + d)
                for d in MEDIA_DOMAINS
            )
        )

    except (ValueError, TypeError):
        return False


def public_dns(host):
    try:
        entries = socket.getaddrinfo(
            host,
            443,
            type=socket.SOCK_STREAM,
        )

        return bool(entries) and all(
            ipaddress.ip_address(
                e[4][0]
            ).is_global
            for e in entries
        )

    except (OSError, ValueError):
        return False


def create_download_token(url):
    if not allowed_media_url(url):
        return None

    now = time.monotonic()

    for token, entry in list(
        download_tokens.items()
    ):
        if entry[1] < now:
            download_tokens.pop(token, None)

    token = secrets.token_urlsafe(32)

    download_tokens[token] = (
        url,
        now + DOWNLOAD_TTL,
    )

    return token


@app.get("/api/file")
async def get_file(
    token: str = Query(..., min_length=20)
):
    entry = download_tokens.get(token)

    if (
        not entry
        or entry[1] < time.monotonic()
    ):
        raise HTTPException(
            status_code=410,
            detail=(
                "Link expirado. "
                "Busque o vídeo novamente."
            ),
        )

    url = entry[0]

    if (
        not allowed_media_url(url)
        or not await asyncio.to_thread(
            public_dns,
            urlparse(url).hostname,
        )
    ):
        raise HTTPException(
            status_code=403,
            detail=(
                "Origem de vídeo não permitida."
            ),
        )

    client = httpx.AsyncClient(
        timeout=httpx.Timeout(
            connect=10,
            read=30,
            write=10,
            pool=10,
        ),
        follow_redirects=False,
        trust_env=False,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://www.tiktok.com/",
            "Accept": "video/mp4,*/*",
        },
    )

    try:
        response = await client.send(
            client.build_request(
                "GET",
                url,
            ),
            stream=True,
        )

        if response.status_code != 200:
            status = response.status_code

            await response.aclose()
            await client.aclose()

            raise HTTPException(
                status_code=502,
                detail=(
                    f"CDN retornou HTTP {status}."
                ),
            )

        content_type = response.headers.get(
            "content-type",
            "",
        ).lower()

        length = response.headers.get(
            "content-length",
            "",
        )

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
                detail=(
                    "A origem não retornou vídeo."
                ),
            )

        if (
            length.isdigit()
            and int(length) > MAX_FILE_BYTES
        ):
            await response.aclose()
            await client.aclose()

            raise HTTPException(
                status_code=413,
                detail=(
                    "Arquivo excede o limite."
                ),
            )

    except HTTPException:
        raise

    except httpx.HTTPError:
        await client.aclose()

        raise HTTPException(
            status_code=502,
            detail=(
                "Falha ao acessar a CDN de vídeo."
            ),
        )

    async def chunks():
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

    return StreamingResponse(
        chunks(),
        media_type="video/mp4",
        headers={
            "Content-Disposition": (
                'attachment; '
                'filename="tiksave-video.mp4"'
            ),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.get("/api/highlights")
async def get_highlights(
    username: str = Query(..., min_length=1),
    collection: str = Query(""),
    cursor: str = Query("0"),
):
    global rate_limit_until

    username = clean_username(username)

    if collection and not re.fullmatch(
        r"[0-9]{1,30}",
        collection,
    ):
        raise HTTPException(
            status_code=400,
            detail="Coleção inválida.",
        )

    if not re.fullmatch(
        r"[0-9]{1,30}",
        cursor,
    ):
        raise HTTPException(
            status_code=400,
            detail="Cursor inválido.",
        )

    api_url = os.getenv(
        "HIGHLIGHTS_API_URL",
        "",
    ).strip()

    method = os.getenv(
        "HIGHLIGHTS_API_METHOD",
        "GET",
    ).strip().upper()

    if (
        not api_url
        or not api_url.startswith("https://")
        or method not in ("GET", "POST")
    ):
        raise HTTPException(
            status_code=503,
            detail=(
                "Fonte de Destaques "
                "não configurada corretamente."
            ),
        )

    cache_key = (
        username,
        collection,
        cursor,
    )

    cached = cache_get(cache_key)

    if cached is not None:
        return cached

    if time.monotonic() < rate_limit_until:
        raise HTTPException(
            status_code=503,
            detail=(
                "Fonte temporariamente limitada. "
                "Tente novamente mais tarde."
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
                    else {
                        "username": username,
                    }
                )

                response = await client.post(
                    api_url,
                    json=body,
                    headers={
                        "Accept": "application/json",
                        "Content-Type": (
                            "application/json"
                        ),
                    },
                )

            else:
                params = {
                    "username": username,
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
                        "Accept": "application/json",
                    },
                )

            if response.status_code == 429:
                retry_after = response.headers.get(
                    "Retry-After",
                    "",
                )

                rate_limit_until = (
                    time.monotonic()
                    + max(
                        RATE_LIMIT_WAIT,
                        int(retry_after)
                        if retry_after.isdigit()
                        else RATE_LIMIT_WAIT,
                    )
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
                "os Destaques."
            ),
        )

    except (ValueError, TypeError):
        raise HTTPException(
            status_code=502,
            detail=(
                "A fonte retornou dados inválidos."
            ),
        )
