            continue  # Direct download must contain audio and video.
        if fmt.get("protocol") not in (None, "https", "http"):
            continue
        output.append({"url": url, "width": int(fmt.get("width") or 0), "height": int(fmt.get("height") or 0), "bitrate_kbps": float(fmt.get("tbr") or 0), "source": "yt-dlp"})
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
    return {"status": "online", "service": "TikSave HD", "version": "2.9.1", "download_source": "yt-dlp / SSSTik", "diagnostic_enabled": True}

@app.post("/api/download")
async def download_video(data: VideoRequest):
    if not valid_tiktok_url(data.url):
        raise HTTPException(status_code=400, detail="Insira um link HTTPS válido do TikTok.")
    diagnostic = {"yt_dlp_padrao": "nao_consultado", "ssstik": "nao_consultado"}
    candidates = []
    try:
        candidates = await asyncio.wait_for(asyncio.to_thread(extract_ytdlp_formats, data.url), timeout=30)
        diagnostic["yt_dlp_padrao"] = f"{len(candidates)}_formatos_encontrados"
    except asyncio.TimeoutError:
        diagnostic["yt_dlp_padrao"] = "tempo_esgotado"
    except Exception as exc:
        diagnostic["yt_dlp_padrao"] = classify_ytdlp_error(exc)
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130.0.0.0 Safari/537.36", "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8"}
    try:
        async with httpx.AsyncClient(timeout=25, follow_redirects=True, headers=headers) as client:
            ssstik_links = await try_ssstik(client, data.url)
        diagnostic["ssstik"] = f"{len(ssstik_links)}_links_encontrados"
        for link in ssstik_links:
            if any(x in link["label"].lower() for x in ("mp3", "audio", "áudio")):
                continue
            candidates.append({"url": link["url"], "source": "ssstik", "label": link["label"], "width": 0, "height": 0, "bitrate_kbps": 0})
    except httpx.HTTPStatusError as exc:
        diagnostic["ssstik"] = f"http_{exc.response.status_code}"
    except httpx.HTTPError as exc:
        diagnostic["ssstik"] = "erro_" + type(exc).__name__
    if not candidates:
        raise HTTPException(status_code=502, detail={"message": "Nenhum provedor retornou links.", "diagnostic": diagnostic})
    # Never fetch provider-supplied media URLs from the backend: they may
    # redirect to private network addresses. The client can inspect metadata
    # for a video it has chosen to play, where browser restrictions permit.
    unique = {}
    for c in candidates:
        if c["source"] == "yt-dlp" and c["width"] and c["height"]:
            # Multiple CDN hosts can point to the same rendition.
            key = ("yt-dlp", c["width"], c["height"], round(c["bitrate_kbps"]))
        else:
            key = (c["source"], c["url"])
        unique.setdefault(key, c)
    candidates = list(unique.values())
    candidates.sort(
        key=lambda c: (c["width"] * c["height"], c["bitrate_kbps"]),
        reverse=True,
    )
    links = []
    for c in candidates[:12]:
        width, height = c["width"], c["height"]
        quality = f"{width}x{height}" if width and height else "qualidade não verificada"
        label = f"{c['source']} • {quality}"
        links.append({
            "url": c["url"], "label": label, "source": c["source"],
            "width": width or None, "height": height or None,
            "bitrate_kbps": c["bitrate_kbps"] or None,
            "quality_verified": bool(width and height),
        })
    return {
        "status": "ok",
        "source": "multiple" if len(set(c["source"] for c in candidates)) > 1 else candidates[0]["source"],
        "hd_available": any(c["width"] >= 720 and c["height"] >= 720 for c in candidates),
        "links": links,
        "diagnostic": diagnostic,
        "note": "Qualidade não verificada não significa qualidade baixa. A melhor resolução não garante o melhor resultado visual. Links podem expirar."
    }

@app.get("/api/highlights")
async def get_highlights(username: str = Query(..., min_length=1), collection: str = Query(""), cursor: str = Query("0")):
    global rate_limit_until
    username = clean_username(username)
    if collection and not re.fullmatch(r"[0-9]{1,30}", collection):
        raise HTTPException(status_code=400, detail="Coleção inválida.")
    if not re.fullmatch(r"[0-9]{1,30}", cursor):
        raise HTTPException(status_code=400, detail="Cursor inválido.")
    api_url = os.getenv("HIGHLIGHTS_API_URL", "").strip()
    method = os.getenv("HIGHLIGHTS_API_METHOD", "GET").strip().upper()
    if not api_url or not api_url.startswith("https://") or method not in ("GET", "POST"):
        raise HTTPException(status_code=503, detail="Fonte de Destaques não configurada corretamente.")
    cache_key = (username, collection, cursor)
    cached = cache_get(cache_key)
    if cached is not None:
        return cached
    if time.monotonic() < rate_limit_until:
        raise HTTPException(status_code=503, detail="Fonte temporariamente limitada. Tente novamente mais tarde.")
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
            if method == "POST":
                body = {"action": "posts", "highlightId": collection, "cursor": cursor} if collection else {"username": username}
                response = await client.post(api_url, json=body, headers={"Accept": "application/json", "Content-Type": "application/json"})
            else:
                params = {"username": username}
                if collection:
                    params.update({"collection": collection, "cursor": cursor})
                response = await client.get(api_url, params=params, headers={"Accept": "application/json"})
            if response.status_code == 429:
                retry_after = response.headers.get("Retry-After", "")
                rate_limit_until = time.monotonic() + max(RATE_LIMIT_WAIT, int(retry_after) if retry_after.isdigit() else RATE_LIMIT_WAIT)
                raise HTTPException(status_code=503, detail="Limite de consultas do provedor atingido.")
            response.raise_for_status()
            payload = response.json()
        result = normalize_items(payload) if collection else normalize_collections(payload)
        if collection:
            result.update({"username": username, "collection": collection})
        cache_set(cache_key, result, ITEMS_TTL if collection else COLLECTIONS_TTL)
        return result
    except HTTPException:
        raise
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=502, detail=f"A fonte de Destaques retornou HTTP {exc.response.status_code}.")
    except httpx.HTTPError:
