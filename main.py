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
