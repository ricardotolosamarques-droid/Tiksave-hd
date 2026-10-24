
import os
import re
import time
from urllib.parse import urlparse, urljoin

import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

from highlights_adapter import (
    normalize_collections,
    normalize_items,
)

app = FastAPI(
    title="TikSave HD API",
    version="2.6"
)

SSSTIK_HOME = "https://ssstik.io/"
SSSTIK_ENDPOINT = "https://ssstik.io/abc?url=dl"
TIKDOWNLOADER_HOME = "https://tikdownloader.io/"
TIKDOWNLOADER_ENDPOINT = "https://tikdownloader.io/api/ajaxSearch"

COLLECTIONS_TTL = 86400
ITEMS_TTL = 1800
RATE_LIMIT_WAIT = 300

cache = {}
rate_limit_until = 0


class VideoRequest(BaseModel):
    url: str


def valid_tiktok_url(value):
    try:
        parsed = urlparse(value)
        host = (parsed.hostname or "").lower()

        return (
            parsed.scheme == "https"
            and (
                host == "tiktok.com"
                or host.endswith(".tiktok.com")
            )
            and not parsed.username
            and not parsed.password
        )
    except (ValueError, TypeError):
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

    for image in image_sources:
        if isinstance(image, dict):
            url = (
                first_url(image.get("display_image"))
                or first_url(
                    image.get("owner_watermark_image")
                )
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


def clean_username(value):
    value = value.strip()

    if "tiktok.com/@" in value:
        value = value.split("tiktok.com/@", 1)[1]
        value = value.split("?", 1)[0].split("/", 1)[0]

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
        r"""value=["']([^"']+)["'][^>]*name=["']tt["']""",
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

    for element in soup.select(
        "a[href], a[data-href], "
        "a[data-url], a[data-download], "
        "button[data-href], button[data-url]"
    ):
        href = ""

        for attribute in (
            "href",
            "data-href",
            "data-url",
            "data-download"
        ):
            value = element.get(attribute)

            if isinstance(value, str) and value.strip():
                href = value.strip()
                break

        if not href:
            continue

        href = urljoin(SSSTIK_HOME, href)
        href = safe_media_url(href)

        if not href or href in seen:
            continue

        label = element.get_text(" ", strip=True)

        title = element.get("title", "")
        aria = element.get("aria-label", "")

        description = " ".join([
            label,
            str(title),
            str(aria),
            str(element.get("class", "")),
        ]).lower()

        if not any(
            keyword in description
            for keyword in (
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
        ):
            continue

        if href.startswith(SSSTIK_HOME):
            parsed = urlparse(href)

            if parsed.path in (
                "/",
                "/abc",
                "/download"
            ):
                continue

        seen.add(href)

        if not label:
            label = (
                str(aria)
                or str(title)
                or "Baixar vídeo"
            )

        links.append({
            "url": href,
            "label": label
        })

    def priority(item):
        label = item["label"].lower()

        if re.search(
            r"\bhd\b|1080p|high quality",
            label
        ):
            return 0

        if any(
            word in label
            for word in (
                "sem marca",
                "without watermark",
                "no watermark"
            )
        ):
            return 1

        if any(
            word in label
            for word in (
                "mp3",
                "audio",
                "áudio"
            )
        ):
            return 3

        return 2

    links.sort(key=priority)

    return links


def extract_tikdownloader_links(html):
    soup = BeautifulSoup(html, "html.parser")
    links = []
    seen = set()

    for anchor in soup.select("a[href], a[data-href]"):
        raw = anchor.get("href") or anchor.get("data-href")

        if not isinstance(raw, str) or not raw.strip():
            continue

        href = safe_media_url(
            urljoin(TIKDOWNLOADER_HOME, raw.strip())
        )

        if not href or href in seen:
            continue

        label = " ".join(filter(None, [
            anchor.get_text(" ", strip=True),
            str(anchor.get("title", "")),
            str(anchor.get("aria-label", "")),
        ])).strip()

        lowered = label.lower()

        if not any(word in lowered for word in (
            "download", "baixar", "mp4",
            "hd", "1080", "watermark", "marca"
        )):
            continue

        if any(word in lowered for word in (
            "mp3", "audio", "áudio"
        )):
            continue

        if (
            urlparse(href).hostname == "tikdownloader.io"
            and urlparse(href).path in ("/", "/pt")
        ):
            continue

        seen.add(href)

        links.append({
            "url": href,
            "label": label or "Baixar vídeo"
        })

    links.sort(
        key=lambda item: not bool(
            re.search(
                r"\bhd\b|1080p|full\s*hd",
                item["label"],
                re.I
            )
        )
    )

    return links


async def try_tikdownloader(client, video_url):
    response = await client.post(
        TIKDOWNLOADER_ENDPOINT,
        data={
            "q": video_url,
            "lang": "pt"
        },
        headers={
            "Origin": "https://tikdownloader.io",
            "Referer": "https://tikdownloader.io/pt",
            "Accept": "application/json, text/html, */*",
            "X-Requested-With": "XMLHttpRequest",
        },
    )

    response.raise_for_status()

    html = response.text

    try:
        payload = response.json()

        if isinstance(payload, dict):
            html = str(
                payload.get("data")
                or payload.get("html")
                or ""
            )

    except ValueError:
        pass

    return extract_tikdownloader_links(html)


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

    return extract_ssstik_links(response.text)


@app.get("/")
def home():
    return {
        "status": "online",
        "service": "TikSave HD",
        "version": "2.6",
        "download_source": "TikDownloader / SSSTik",
        "diagnostic_enabled": True
    }


@app.post("/api/download")
async def download_video(data: VideoRequest):
    if not valid_tiktok_url(data.url):
        raise HTTPException(
            status_code=400,
            detail="Insira um link HTTPS válido do TikTok."
        )

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/130.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
    }

    diagnostic = {
        "tikdownloader": "nao_consultado",
        "ssstik": "nao_consultado"
    }

    async with httpx.AsyncClient(
        timeout=30,
        follow_redirects=True,
        headers=headers
    ) as client:

        try:
            links = await try_tikdownloader(
                client, data.url
            )

            has_hd = any(
                re.search(
                    r"\bhd\b|1080p|full\s*hd",
                    item["label"],
                    re.I
                )
                for item in links
            )

            diagnostic["tikdownloader"] = (
                "hd_encontrado" if has_hd
                else f"sem_hd_reconhecido_{len(links)}_links"
            )

            if has_hd:
                return {
                    "status": "ok",
                    "source": "tikdownloader",
                    "hd_available": True,
                    "links": links,
                    "diagnostic": diagnostic
                }

        except httpx.HTTPStatusError as exc:
            diagnostic["tikdownloader"] = (
                f"http_{exc.response.status_code}"
            )

        except httpx.RequestError as exc:
            diagnostic["tikdownloader"] = (
                f"erro_conexao_{type(exc).__name__}"
            )

        except (ValueError, TypeError) as exc:
            diagnostic["tikdownloader"] = (
                f"erro_resposta_{type(exc).__name__}"
            )

        try:
            links = await try_ssstik(
                client, data.url
            )

            has_hd = any(
                re.search(
                    r"\bhd\b|1080p|high quality",
                    item["label"],
                    re.I
                )
                for item in links
            )

            diagnostic["ssstik"] = (
                f"{len(links)}_links_encontrados"
            )

            if links:
                return {
                    "status": "ok",
                    "source": "ssstik",
                    "hd_available": bool(has_hd),
                    "links": links,
                    "diagnostic": diagnostic
                }

        except httpx.HTTPStatusError as exc:
            diagnostic["ssstik"] = (
                f"http_{exc.response.status_code}"
            )

        except httpx.RequestError as exc:
            diagnostic["ssstik"] = (
                f"erro_conexao_{type(exc).__name__}"
            )

        except (ValueError, TypeError) as exc:
            diagnostic["ssstik"] = (
                f"erro_resposta_{type(exc).__name__}"
            )

    raise HTTPException(
        status_code=502,
        detail={
            "message": "Nenhum provedor retornou links.",
            "diagnostic": diagnostic
        }
    )


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
        "HIGHLIGHTS_API_URL", ""
    ).strip()

    method = os.getenv(
        "HIGHLIGHTS_API_METHOD",
        "GET"
    ).strip().upper()

    if not api_url:
        raise HTTPException(
            status_code=503,
            detail=(
                "A fonte de Destaques "
                "não está configurada."
            )
        )

    if not api_url.startswith("https://"):
        raise HTTPException(
            status_code=503,
            detail=(
                "A fonte de Destaques "
                "precisa usar HTTPS."
            )
        )

    if method not in ("GET", "POST"):
        raise HTTPException(
            status_code=503,
            detail="Método de Destaques inválido."
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
                "A fonte retornou dados inválidos."
            )
        )
