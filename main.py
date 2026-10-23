
import os
import re
import time
import asyncio
from urllib.parse import urlparse

import httpx
from highlights_adapter import normalize_collections, normalize_items
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

app = FastAPI(title="TikSave HD API")

# Cache dos Destaques
COLLECTIONS_TTL = 24 * 60 * 60  # 24 horas
ITEMS_TTL = 30 * 60            # 30 minutos
RATE_LIMIT_WAIT = 5 * 60       # 5 minutos

highlights_cache = {}
highlights_lock = asyncio.Lock()
rate_limit_until = 0.0


class VideoRequest(BaseModel):
    url: str


def valid_tiktok_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        return (
            parsed.scheme == "https"
            and (
                host == "tiktok.com"
                or host.endswith(".tiktok.com")
            )
        )
    except ValueError:
        return False


def safe_media_url(value):
    if not isinstance(value, str):
        return None

    try:
        parsed = urlparse(value)
        if (
            parsed.scheme == "https"
            and parsed.hostname
            and not parsed.username
            and not parsed.password
        ):
            return value
    except ValueError:
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

    # Vídeos
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

    # Fotos
    image_sources = []

    for field in ("images", "image_post_info"):
        value = story.get(field)

        if isinstance(value, dict):
            value = value.get("images", [])

        if isinstance(value, list):
            image_sources.extend(value)

    images = []

    for image in image_sources:
        if isinstance(image, dict):
            url = (
                first_url(image.get("display_image"))
                or first_url(image.get("owner_watermark_image"))
                or first_url(image.get("url_list"))
                or first_url(image.get("url"))
            )
        else:
            url = first_url(image)

        if url and url not in images:
            images.append(url)

    if images:
        results.append({
            "type": "image",
            "images": images,
            "downloadAllowed": True
        })

    return results


def clean_username(value: str) -> str:
    value = value.strip()

    if "tiktok.com/@" in value:
        value = value.split("tiktok.com/@", 1)[1]
        value = value.split("?", 1)[0].split("/", 1)[0]

    value = value.lstrip("@")

    if not re.fullmatch(r"[A-Za-z0-9._]{2,24}", value):
        raise HTTPException(
            status_code=400,
            detail="Usuário inválido."
        )

    return value


def cache_get(key):
    entry = highlights_cache.get(key)

    if not entry:
        return None

    if time.monotonic() >= entry["expires"]:
        highlights_cache.pop(key, None)
        return None

    return entry["data"]


def cache_set(key, data, ttl):
    now = time.monotonic()

    # Remove entradas expiradas
    expired = [
        k for k, v in highlights_cache.items()
        if v["expires"] <= now
    ]

    for k in expired:
        highlights_cache.pop(k, None)

    highlights_cache[key] = {
        "data": data,
        "expires": now + ttl
    }


@app.get("/")
def home():
    return {
        "status": "online",
        "service": "TikSave HD",
        "version": "2.1"
    }


# Sistema de download mantido
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

        soup = BeautifulSoup(
            response.text,
            "html.parser"
        )

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
                detail="Nenhum link encontrado."
            )

        return {
            "status": "ok",
            "links": links
        }

    except httpx.HTTPError:
        raise HTTPException(
            status_code=502,
            detail="Serviço de download indisponível."
        )


# API de Destaques com cache
@app.get("/api/highlights")
async def get_highlights(
    username: str = Query(..., min_length=1),
    collection: str = Query(""),
    cursor: str = Query("0"),
):
    global rate_limit_until

    username = clean_username(username)

    if collection and not re.fullmatch(r"[0-9]{1,30}", collection):
        raise HTTPException(
            status_code=400,
            detail="Coleção inválida."
        )

    if not re.fullmatch(r"[0-9]{1,30}", cursor):
        raise HTTPException(
            status_code=400,
            detail="Cursor inválido."
        )

    cache_key = (username.lower(), collection, cursor)

    # Primeiro tenta usar o cache
    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    api_url = os.getenv("HIGHLIGHTS_API_URL", "").strip()
    method = os.getenv(
        "HIGHLIGHTS_API_METHOD", "POST"
    ).strip().upper()

    if not api_url:
        raise HTTPException(
            status_code=503,
            detail="Fonte de Destaques não configurada."
        )

    parsed = urlparse(api_url)

    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise HTTPException(
            status_code=503,
            detail="Fonte de Destaques inválida."
        )

    if method not in ("GET", "POST"):
        raise HTTPException(
            status_code=503,
            detail="Método de consulta inválido."
        )

    # Impede várias consultas simultâneas ao provedor
    async with highlights_lock:

        # Outra requisição pode ter preenchido o cache
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
                    params = {"username": username}

                    if collection:
                        params["collection"] = collection
                        params["cursor"] = cursor

                    response = await client.get(
                        api_url,
                        params=params,
                        headers={
                            "Accept": "application/json"
                        }
                    )

                if response.status_code == 429:
                    # Respeita o limite, sem tentar contorná-lo
                    wait_seconds = RATE_LIMIT_WAIT

                    retry_after = response.headers.get(
                        "Retry-After", ""
                    )

                    if retry_after.isdigit():
                        wait_seconds = max(
                            wait_seconds,
                            int(retry_after)
                        )

                    rate_limit_until = (
                        time.monotonic() + wait_seconds
                    )

                    raise HTTPException(
                        status_code=503,
                        detail=(
                            "Limite de consultas do provedor "
                            "atingido. Tente mais tarde."
                        )
                    )

                response.raise_for_status()
                payload = response.json()

            if collection:
                result = normalize_items(payload)
                result["username"] = username
                result["collection"] = collection

                ttl = ITEMS_TTL
            else:
                result = normalize_collections(payload)

                ttl = COLLECTIONS_TTL

            cache_set(cache_key, result, ttl)

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
                detail="Não foi possível consultar os Destaques."
            )

        except (ValueError, TypeError):
            raise HTTPException(
                status_code=502,
                detail="A fonte retornou dados inválidos."
            )
