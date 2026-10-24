"""Adaptador de dados de Destaques para o TikSave HD.

Este módulo não acessa APIs de terceiros. Recebe dados já obtidos de uma
fonte autorizada e normaliza-os para uso pelo front-end.
"""
from urllib.parse import urlparse


def https_url(value):
    if not isinstance(value, str):
        return None
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        return None
    return value


def normalize_collections(payload):
    if not isinstance(payload, dict):
        raise ValueError("Resposta de coleções inválida")
    collections = payload.get("highlights", [])
    if not isinstance(collections, list):
        raise ValueError("Campo highlights inválido")
    result = []
    for item in collections:
        if not isinstance(item, dict):
            continue
        identifier = str(item.get("id") or "")
        if not identifier.isdigit():
            continue
        result.append({
            "id": identifier,
            "title": item.get("title") or "Destaque",
            "cover": https_url(item.get("cover")),
            "total_stories": item.get("count") if isinstance(item.get("count"), int) else None,
        })
    return {"username": payload.get("username", ""), "collections": result}


def normalize_items(payload):
    if not isinstance(payload, dict):
        raise ValueError("Resposta de itens inválida")
    source_items = payload.get("items", [])
    if not isinstance(source_items, list):
        raise ValueError("Campo items inválido")
    items = []
    for item in source_items:
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("id") or "")
        video = https_url(item.get("videoUrl"))
        images = item.get("images") or []
        if not isinstance(images, list):
            images = []
        image_urls = []
        for image in images:
            url = https_url(image) if isinstance(image, str) else https_url(image.get("url")) if isinstance(image, dict) else None
            if url and url not in image_urls:
                image_urls.append(url)
        if video:
            items.append({"id": item_id, "type": "video", "url": video,
                          "cover": https_url(item.get("cover")), "downloadAllowed": None})
        if image_urls:
            items.append({"id": item_id, "type": "image", "images": image_urls,
                          "cover": https_url(item.get("cover")), "downloadAllowed": None})
    cursor = payload.get("nextCursor")
    return {"collection": str(payload.get("highlightId") or ""),
            "items": items,
            "next_cursor": str(cursor) if payload.get("hasMore") and cursor is not None else None}
