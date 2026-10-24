            download_tokens.pop(key, None)
    token = secrets.token_urlsafe(32)
    download_tokens[token] = (url, now + DOWNLOAD_TTL)
    return token


@app.get("/api/file")
async def get_file(token: str = Query(..., min_length=20)):
    entry = download_tokens.get(token)
    if not entry or entry[1] < time.monotonic():
        raise HTTPException(status_code=410, detail="Link expirado. Busque o vídeo novamente.")
    url = entry[0]
    if not allowed_media_url(url) or not await asyncio.to_thread(public_dns, urlparse(url).hostname):
        raise HTTPException(status_code=403, detail="Origem de vídeo não permitida.")
    client = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=10, read=30, write=10, pool=10),
        follow_redirects=False, trust_env=False,
        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.tiktok.com/", "Accept": "video/mp4,*/*"},
    )
    try:
        response = await client.send(client.build_request("GET", url), stream=True)
        if response.status_code != 200:
            status = response.status_code
            await response.aclose()
            await client.aclose()
            raise HTTPException(status_code=502, detail=f"CDN retornou HTTP {status}.")
        content_type = response.headers.get("content-type", "").lower()
        length = response.headers.get("content-length", "")
        if not (content_type.startswith("video/") or content_type.startswith("application/octet-stream")):
            await response.aclose()
            await client.aclose()
            raise HTTPException(status_code=502, detail="A origem não retornou vídeo.")
        if length.isdigit() and int(length) > MAX_FILE_BYTES:
            await response.aclose()
            await client.aclose()
            raise HTTPException(status_code=413, detail="Arquivo excede o limite.")
    except HTTPException:
        raise
    except httpx.HTTPError:
        await client.aclose()
        raise HTTPException(status_code=502, detail="Falha ao acessar a CDN de vídeo.")

    async def chunks():
        total = 0
        try:
            async for chunk in response.aiter_bytes(chunk_size=65536):
                total += len(chunk)
                if total > MAX_FILE_BYTES:
                    break
                yield chunk
        finally:
            await response.aclose()
            await client.aclose()

    return StreamingResponse(chunks(), media_type="video/mp4", headers={
        "Content-Disposition": 'attachment; filename="tiksave-video.mp4"',
        "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"
    })

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
        raise HTTPException(status_code=502, detail="Não foi possível consultar os Destaques.")
    except (ValueError, TypeError):
        raise HTTPException(status_code=502, detail="A fonte retornou dados inválidos.")
