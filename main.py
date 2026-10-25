import asyncio
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
import httpx
import yt_dlp

app = FastAPI(title="TikSave HD Backend", version="2.0")

# Dicionário de tokens para o proxy (ajuste conforme a sua implementação atual)
download_tokens = {}

@app.post("/api/download")
async def api_download(payload: dict):
    url = payload.get("url")
    if not url:
        raise HTTPException(status_code=400, detail="URL não fornecida.")
    
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
    }
    
    def extract():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(url, download=False)
            
    try:
        info = await asyncio.to_thread(extract)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro ao extrair vídeo: {str(e)}")
        
    formats = []
    for f in info.get('formats', []):
        # BLOQUEIO DE ÁUDIO: Ignora formatos que possuem apenas áudio (sem vídeo)
        if f.get('vcodec') == 'none':
            continue
            
        formats.append({
            "format_id": f.get("format_id"),
            "ext": f.get("ext"),
            "url": f.get("url"),
            "resolution": f.get("resolution") or f"{f.get('width', 0)}x{f.get('height', 0)}"
        })
        
    return {
        "success": True,
        "title": info.get("title"),
        "formats": formats
    }

@app.get("/api/file")
async def proxy_file(token: str):
    target_url = download_tokens.get(token)
    if not target_url:
        raise HTTPException(status_code=404, detail="Token inválido ou expirado.")

    async with httpx.AsyncClient(follow_redirects=True) as client:
        try:
            response = await client.get(target_url, headers={"Range": "bytes=0-1023"}, timeout=10.0)
            content_type = response.headers.get("content-type", "").lower()
            
            # BLOQUEIO DE ÁUDIO NO PROXY: Rejeita se o Content-Type for de áudio
            if "audio/" in content_type:
                raise HTTPException(
                    status_code=403, 
                    detail="O descarregamento de ficheiros de áudio não é permitido."
                )
        except Exception as e:
            if isinstance(e, HTTPException):
                raise e
            raise HTTPException(status_code=500, detail="Erro ao validar arquivo.")
            
    # Realiza o streaming do vídeo aprovado
    async def stream_generator():
        async with httpx.AsyncClient(follow_redirects=True) as c:
            async with c.stream("GET", target_url) as r:
                async for chunk in r.aiter_bytes():
                    yield chunk

    return StreamingResponse(stream_generator(), media_type="video/mp4")

@app.get("/api/highlights")
async def get_highlights(username: str):
    clean_user = username.strip("@")
    profile_url = f"https://www.tiktok.com/@{clean_user}"
    
    ydl_opts = {
        'extract_flat': True,
        'skip_download': True,
        'quiet': True,
    }
    
    def extract_profile():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(profile_url, download=False)
            
    try:
        info = await asyncio.to_thread(extract_profile)
        entries = info.get('entries', [])
        
        highlights = []
        for entry in entries[:20]:
            highlights.append({
                "id": entry.get("id"),
                "title": entry.get("title", ""),
                "url": entry.get("url") or f"https://www.tiktok.com/@{clean_user}/video/{entry.get('id')}",
                "thumbnail": entry.get("thumbnail"),
                "duration": entry.get("duration"),
            })
            
        return {"success": True, "highlights": highlights}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro ao buscar destaques: {str(e)}")
