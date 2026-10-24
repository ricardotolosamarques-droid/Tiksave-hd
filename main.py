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
MEDIA_DOMAINS = ("tiktok.com", "tiktokcdn.com", "tiktokcdn-us.com", "byteoversea.com", "ibytedtos.com", "byteimg.com", "muscdn.com", "musical.ly")
rate_limit_until = 0

class VideoRequest(BaseModel):
    url: str

def valid_tiktok_url(value):
    try:
        p = urlparse(value)
        host = (p.hostname or "").lower()
        return p.scheme == "https" and (host == "tiktok.com" or host.endswith(".tiktok.com")) and not p.username and not p.password
    except (ValueError, TypeError):
        return False

def safe_media_url(value):
    if not isinstance(value, str):
        return None
    try:
        p = urlparse(value)
        if p.scheme == "https" and p.hostname and not p.username and not p.password:
            return value
    except (ValueError, TypeError):
        pass
    return None

def first_url(value):
    if isinstance(value, str):
        return safe_media_url(value)
    if isinstance(value, list):
        return next((v for x in value if (v := first_url(x))), None)
    if isinstance(value, dict):
        return next((v for k in ("url_list", "url", "uri") if (v := first_url(value.get(k)))), None)
    return None

def extract_media(story):
    if not isinstance(story, dict):
        return []
    results = []
    video = story.get("video") or {}
    if isinstance(video, dict):
        url = first_url(video.get("play_addr")) or first_url(video.get("download_addr")) or first_url(video.get("url"))
        if url:
            results.append({"type": "video", "url": url, "downloadAllowed": True})
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
            url = first_url(item.get("display_image")) or first_url(item.get("owner_watermark_image")) or first_url(item.get("url_list")) or first_url(item.get("url"))
        else:
            url = first_url(item)
        if url and url not in images:
            images.append(url)
    if images:
        results.append({"type": "image", "images": images, "downloadAllowed": True})
    return results

def clean_username(value):
    value = value.strip()
    if "tiktok.com/@" in value:
        value = value.split("tiktok.com/@", 1)[1].split("?", 1)[0].split("/", 1)[0]
    value = value.lstrip("@")
    if not re.fullmatch(r"[A-Za-z0-9._]{2,24}", value):
        raise HTTPException(status_code=400, detail="Nome de usuário inválido.")
    return value
