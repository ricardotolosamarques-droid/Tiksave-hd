
import os
import re
import time
import asyncio
import json
import subprocess
from urllib.parse import urlparse, urljoin

import httpx
import yt_dlp
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

from highlights_adapter import normalize_collections, normalize_items


app = FastAPI(title="TikSave HD API", version="2.9")

SSSTIK_HOME = "https://ssstik.io/"
SSSTIK_ENDPOINT = "https://ssstik.io/abc?url=dl"

COLLECTIONS_TTL = 86400
ITEMS_TTL = 1800
RATE_LIMIT_WAIT = 300

cache = {}
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
        for item in value:
            found = first_url(item)

            if found:
                return found

    if isinstance(value, dict):
        for key in ("url_list", "url", "uri"):
            found = first_url(value.get(key))

            if found:
                return found

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
                "downloadAllowed": True
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
                or first_url(
                    item.get("owner_watermark_image")
                )
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
            "downloadAllowed": True
        })

    return results


def clean_username(value):
    value = value.strip()

    if "tiktok.com/@" in value:
        value = value.split("tiktok.com/@", 1)[1]
        value = value.split("?", 1)[0]
        value = value.split("/", 1)[0]

    value = value.lstrip("@")

    if not re.fullmatch(
        r"[A-Za-z0-9._]{2,24}",
        value
    ):
        raise HTTPException(
            status_code=400,
            detail="Nome de usuário inválido."
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
    cache[key] = (
        time.monotonic() + ttl,
        value
    )


def extract_ssstik_token(html):
    patterns = [
        r"""s_tt\s*=\s*['"]([^'"]+)['"]""",
        r"""["']tt["']\s*:\s*["']([^"']+)["']""",
        r"""name=["']tt["'][^>]*value=["']([^"']+)["']""",
        r"""value=["']([^'"]+)["'][^>]*name=["']tt["']""",
    ]

    for pattern in patterns:
        match = re.search(
            pattern,
            html,
            re.IGNORECASE
        )

        if match:
            return match.group(1)

    return ""


def extract_ssstik_links(html):
    soup = BeautifulSoup(html, "html.parser")

    links = []
    seen = set()

    selector = (
        "a[href], a[data-href], "
        "a[data-url], a[data-download], "
        "button[data-href], button[data-url]"
    )

    for element in soup.select(selector):
        raw = ""

        for attribute in (
            "href",
            "data-href",
            "data-url",
            "data-download"
        ):
            value = element.get(attribute)

            if isinstance(value, str) and value.strip():
                raw = value.strip()
                break

        if not raw:
            continue

        url = safe_media_url(
            urljoin(SSSTIK_HOME, raw)
        )

        if not url or url in seen:
            continue

        label = (
            element.get_text(" ", strip=True)
            or str(
                element.get("aria-label")
                or element.get("title")
                or "Baixar vídeo"
            )
        )

        description = " ".join([
            label,
            str(element.get("title", "")),
            str(element.get("aria-label", "")),
            str(element.get("class", "")),
        ]).lower()

        keywords = (
            "download",
            "baixar",
            "watermark",
            "marca",
            "hd",
            "1080",
            "quality",
            "mp3",
            "audio",
            "áudio"
        )

        if not any(
            word in description
            for word in keywords
        ):
            continue

        parsed = urlparse(url)

        if (
            parsed.hostname == "ssstik.io"
            and parsed.path in (
                "/",
                "/abc",
                "/download"
            )
        ):
            continue

        seen.add(url)

        links.append({
            "url": url,
            "label": label
        })

    return links


async def try_ssstik(client, video_url):
    page = await client.get(SSSTIK_HOME)
    page.raise_for_status()

    token = extract_ssstik_token(page.text)

    response = await client.post(
        SSSTIK_ENDPOINT,
        data={
            "id": video_url,
            "locale": "pt",
            "tt": token
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

    return extract_ssstik_links(
        response.text
    )


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
            download=False
        )

    if not isinstance(info, dict):
        return []

    formats = info.get("formats") or [info]

    output = []

    for fmt in formats:
        url = safe_media_url(
            fmt.get("url")
        )

        if not url:
            continue

        if fmt.get("vcodec") in (
            "none",
            None
        ):
            continue

        # O download direto precisa conter
        # vídeo e áudio no mesmo arquivo.
        if fmt.get("acodec") == "none":
            continue

        if fmt.get("protocol") not in (
            None,
            "https",
            "http"
        ):
            continue

        output.append({
            "url": url,
            "width": int(
                fmt.get("width") or 0
            ),
            "height": int(
                fmt.get("height") or 0
            ),
            "bitrate_kbps": float(
                fmt.get("tbr") or 0
            ),
            "source": "yt-dlp"
        })

    return output


def probe_media(url):
    """
    Tenta identificar a resolução
    real de um arquivo de vídeo.

    Esta função deve ser usada apenas
    com URLs de mídia confiáveis.
    """

    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                (
                    "stream=codec_type,width,height,"
                    "bit_rate:format=bit_rate"
                ),
                "-of",
                "json",
                url
            ],
            capture_output=True,
            text=True,
            timeout=9,
            check=False,
        )

        if result.returncode:
            return {}

        payload = json.loads(
            result.stdout
        )

        video = next(
            (
                stream
                for stream in payload.get(
                    "streams",
                    []
                )
                if stream.get("codec_type")
                == "video"
            ),
            {}
        )

        bitrate = (
            video.get("bit_rate")
            or payload.get(
                "format",
                {}
            ).get("bit_rate")
            or 0
        )

        return {
            "width": int(
                video.get("width") or 0
            ),
            "height": int(
                video.get("height") or 0
            ),
            "bitrate_kbps": round(
                int(bitrate) / 1000
            )
        }

    except (
        OSError,
        ValueError,
        subprocess.TimeoutExpired
    ):
        return {}


def classify_ytdlp_error(exc):
    message = str(exc).lower()

    if (
        "unexpected response from webpage request"
        in message
    ):
        return "resposta_inesperada_tiktok"

    if (
        "403" in message
        or "forbidden" in message
    ):
        return "http_403"

    if (
        "429" in message
        or "too many requests" in message
    ):
        return "http_429"

    return (
        "erro_" + type(exc).__name__
    )


@app.get("/")
def home():
    return {
        "status": "online",
        "service": "TikSave HD",
        "version": "2.9",
        "download_source": "yt-dlp / SSSTik",
        "diagnostic_enabled": True
    }


@app.post("/api/download")
async def download_video(data: VideoRequest):
    if not valid_tiktok_url(data.url):
        raise HTTPException(
            status_code=400,
            detail=(
                "Insira um link HTTPS "
                "válido do TikTok."
            )
        )

    diagnostic = {
        "yt_dlp_padrao": "nao_consultado",
        "ssstik": "nao_consultado"
    }

    candidates = []

    # Primeiro: consulta o yt-dlp.
    try:
        candidates = await asyncio.wait_for(
            asyncio.to_thread(
                extract_ytdlp_formats,
                data.url
            ),
            timeout=30
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

    # Segundo: consulta o SSSTik mesmo
    # quando o yt-dlp encontra um vídeo.
    headers = {
        "User-Agent": (
            "Mozilla/5.0 "
            "(Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 "
            "Chrome/130.0.0.0 "
            "Safari/537.36"
        ),
        "Accept-Language": (
            "pt-BR,pt;q=0.9,en;q=0.8"
        ),
    }

    try:
        async with httpx.AsyncClient(
            timeout=25,
            follow_redirects=True,
            headers=headers
        ) as client:

            ssstik_links = await try_ssstik(
                client,
                data.url
            )

        diagnostic["ssstik"] = (
            f"{len(ssstik_links)}_links_encontrados"
        )

        for link in ssstik_links:
            label = link["label"].lower()

            if any(
                word in label
                for word in (
                    "mp3",
                    "audio",
                    "áudio"
                )
            ):
                continue

            candidates.append({
                "url": link["url"],
                "source": "ssstik",
                "label": link["label"],
                "width": 0,
                "height": 0,
                "bitrate_kbps": 0
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
                "diagnostic": diagnostic
            }
        )

    # Consulta metadados de até três
    # links retornados pelo SSSTik.
    ssstik_to_probe = [
        item
        for item in candidates
        if item["source"] == "ssstik"
    ][:3]

    probes = await asyncio.gather(
        *(
            asyncio.to_thread(
                probe_media,
                item["url"]
            )
            for item in ssstik_to_probe
        )
    )

    for candidate, metadata in zip(
        ssstik_to_probe,
        probes
    ):
        candidate.update({
            key: value
            for key, value in metadata.items()
            if value
        })

    # Remove links duplicados.
    unique = {}

    for candidate in candidates:
        unique[candidate["url"]] = candidate

    candidates = list(
        unique.values()
    )

    # Ordena por resolução e bitrate
    # quando essas informações existem.
    candidates.sort(
        key=lambda item: (
            bool(item["height"]),
            item["width"] * item["height"],
            item["bitrate_kbps"]
        ),
        reverse=True
    )

    links = []

    for item in candidates[:12]:
        width = item["width"]
        height = item["height"]

        if width and height:
            quality = f"{width}x{height}"

        elif height:
            quality = (
                f"altura {height}px"
            )

        else:
            quality = "não verificada"

        label = (
            f"{item['source']} • {quality}"
        )

        links.append({
            "url": item["url"],
            "label": label,
            "source": item["source"],
            "width": width or None,
            "height": height or None,
            "bitrate_kbps": (
                item["bitrate_kbps"]
                or None
            ),
            "quality_verified": bool(
                width and height
            )
        })

    sources = {
        item["source"]
        for item in candidates
    }

    return {
        "status": "ok",
        "source": (
            "multiple"
            if len(sources) > 1
            else candidates[0]["source"]
        ),
        "hd_available": any(
            item["width"] >= 720
            and item["height"] >= 720
            for item in candidates
        ),
        "links": links,
        "diagnostic": diagnostic,
        "note": (
            "Qualidade não verificada não "
            "significa qualidade baixa. "
            "Links podem expirar."
        )
    }


@app.get("/api/highlights")
async def get_highlights(
    username: str = Query(..., min_length=1),
    collection: str = Query(""),
    cursor: str = Query("0")
):
    global rate_limit_until

    username = clean_username(username)

    if collection and not re.fullmatch(
        r"[0-9]{1,30}",
        collection
    ):
        raise HTTPException(
            status_code=400,
            detail="Coleção inválida."
        )

    if not re.fullmatch(
        r"[0-9]{1,30}",
        cursor
    ):
        raise HTTPException(
            status_code=400,
            detail="Cursor inválido."
        )

    api_url = os.getenv(
        "HIGHLIGHTS_API_URL",
        ""
    ).strip()

    method = os.getenv(
        "HIGHLIGHTS_API_METHOD",
        "GET"
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
            )
        )

    cache_key = (
        username,
        collection,
        cursor
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
            )
        )

    try:
        async with httpx.AsyncClient(
            timeout=30,
            follow_redirects=False
        ) as client:

            if method == "POST":
                if collection:
                    body = {
                        "action": "posts",
                        "highlightId": collection,
                        "cursor": cursor
                    }
                else:
                    body = {
                        "username": username
                    }

                response = await client.post(
                    api_url,
                    json=body,
                    headers={
                        "Accept": "application/json",
                        "Content-Type": "application/json"
                    }
                )

            else:
                params = {
                    "username": username
                }

                if collection:
                    params.update({
                        "collection": collection,
                        "cursor": cursor
                    })

                response = await client.get(
                    api_url,
                    params=params,
                    headers={
                        "Accept": "application/json"
                    }
                )

            if response.status_code == 429:
                retry_after = response.headers.get(
                    "Retry-After",
                    ""
                )

                wait_seconds = (
                    int(retry_after)
                    if retry_after.isdigit()
                    else RATE_LIMIT_WAIT
                )

                rate_limit_until = (
                    time.monotonic()
                    + max(
                        RATE_LIMIT_WAIT,
                        wait_seconds
                    )
                )

                raise HTTPException(
                    status_code=503,
                    detail=(
                        "Limite de consultas "
                        "do provedor atingido."
                    )
                )

            response.raise_for_status()
            payload = response.json()

        if collection:
            result = normalize_items(payload)

            result.update({
                "username": username,
                "collection": collection
            })

            ttl = ITEMS_TTL

        else:
            result = normalize_collections(
                payload
            )

            ttl = COLLECTIONS_TTL

        cache_set(
            cache_key,
            result,
            ttl
        )

        return result

    except HTTPException:
        raise

    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=502,
            detail=(
                "A fonte de Destaques retornou HTTP "
                f"{exc.response.status_code}."
            )
        )

    except httpx.HTTPError:
        raise HTTPException(
            status_code=502,
            detail=(
                "Não foi possível consultar "
                "os Destaques."
            )
        )

    except (ValueError, TypeError):
        raise HTTPException(
            status_code=502,
            detail=(
                "A fonte retornou "
                "dados inválidos."
            )
        )
