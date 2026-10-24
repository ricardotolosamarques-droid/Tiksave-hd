
import os
import re
import time
import asyncio
from urllib.parse import urlparse, urljoin

import httpx
import yt_dlp
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

from highlights_adapter import normalize_collections, normalize_items


app = FastAPI(title="TikSave HD API", version="2.8")

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
            keyword in description
            for keyword in keywords
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


@app.get("/")
def home():
    return {
        "status": "online",
        "service": "TikSave HD",
        "version": "2.8",
        "download_source": "yt-dlp / SSSTik",
        "diagnostic_enabled": True
    }


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


def extract_best_with_ytdlp(
    video_url,
    browser_mode=False
):
    """
    Consulta os formatos disponíveis.
    Não baixa o vídeo no servidor.
    """

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

    if browser_mode:
        # Requer curl_cffi.
        # Pode ajudar com diferenças de HTTP,
        # mas não garante acesso ao TikTok.
        opts["impersonate"] = "chrome"

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(
            video_url,
            download=False
        )

    if not isinstance(info, dict):
        return None

    candidates = []

    for fmt in info.get("formats") or []:
        if not isinstance(fmt, dict):
            continue

        url = safe_media_url(fmt.get("url"))

        if not url:
            continue

        if fmt.get("vcodec") in (None, "none"):
            continue

        if fmt.get("acodec") in (None, "none"):
            continue

        if fmt.get("protocol") not in (
            None,
            "https",
            "http"
        ):
            continue

        candidates.append((
            int(fmt.get("height") or 0),
            float(fmt.get("tbr") or 0),
            url
        ))

    if candidates:
        height, _, url = max(
            candidates,
            key=lambda item: (
                item[0],
                item[1]
            )
        )

        return {
            "url": url,
            "height": height
        }

    direct = safe_media_url(info.get("url"))

    if direct:
        return {
            "url": direct,
            "height": int(
                info.get("height") or 0
            )
        }

    return None


def classify_ytdlp_error(exc):
    """
    Classifica erros sem expor
    links internos na resposta JSON.
    """

    message = str(exc).lower()

    if (
        "unexpected response from webpage request"
        in message
    ):
        return "resposta_inesperada_tiktok"

    if (
        "impersonat" in message
        or "curl_cffi" in message
    ):
        return "impersonacao_indisponivel"

    if (
        "http error 403" in message
        or "forbidden" in message
    ):
        return "http_403"

    if (
        "http error 429" in message
        or "too many requests" in message
    ):
        return "http_429"

    if (
        "login" in message
        or "sign in" in message
    ):
        return "acesso_restrito"

    return f"erro_{type(exc).__name__}"


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
        "yt_dlp_chrome": "nao_consultado",
        "yt_dlp_padrao": "nao_consultado",
        "ssstik": "nao_consultado",
    }

    modes = (
        (True, "yt_dlp_chrome"),
        (False, "yt_dlp_padrao")
    )

    for mode, key in modes:
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(
                    extract_best_with_ytdlp,
                    data.url,
                    mode
                ),
                timeout=30
            )

            if result:
                height = result["height"]

                diagnostic[key] = (
                    f"video_encontrado_"
                    f"{height or 'resolucao_desconhecida'}"
                )

                if height:
                    label = f"Baixar vídeo {height}p"
                else:
                    label = "Baixar vídeo"

                return {
                    "status": "ok",
                    "source": "yt-dlp",
                    "hd_available": height >= 720,
                    "links": [{
                        "url": result["url"],
                        "label": label
                    }],
                    "diagnostic": diagnostic
                }

            diagnostic[key] = "sem_link_compativel"

        except asyncio.TimeoutError:
            diagnostic[key] = "tempo_esgotado"

        except Exception as exc:
            diagnostic[key] = (
                classify_ytdlp_error(exc)
            )

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/130.0.0.0 Safari/537.36"
        ),
        "Accept-Language": (
            "pt-BR,pt;q=0.9,en;q=0.8"
        ),
    }

    async with httpx.AsyncClient(
        timeout=30,
        follow_redirects=True,
        headers=headers
    ) as client:

        try:
            links = await try_ssstik(
                client,
                data.url
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
            "message": (
                "Nenhum provedor retornou links."
            ),
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
        "HIGHLIGHTS_API_URL",
        ""
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
                    "Retry-After",
                    ""
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
