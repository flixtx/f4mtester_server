
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
import urllib.parse
import requests
import binascii
import os
import re
from urllib.parse import urljoin
from anyio import to_thread
from requests.exceptions import ConnectionError, RequestException
from urllib3.exceptions import IncompleteRead
import time
import logging

logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DEFAULT_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
IP_CACHE_TS = {}
IP_CACHE_MP4 = {}
AGENT_OF_CHAOS = {}
COUNT_CLEAR = {}

def get_ip(request):
    forwarded_for = request.headers.get("x-forwarded-for")
    real_ip = request.headers.get("x-real-ip")
    if forwarded_for:
        ip = forwarded_for.split(",")[0].strip()
    elif real_ip:
        ip = real_ip
    else:
        ip = request.client.host
    return ip

def get_cache_key(client_ip: str, url: str) -> str:
    return f"{client_ip}:{url}"

def rewrite_m3u8_urls(playlist_content: str, base_url: str, request: Request) -> str:
    def replace_url(match):
        segment_url = match.group(0).strip()
        if segment_url.startswith('#') or not segment_url or segment_url == '/':
            return segment_url
        try:
            absolute_url = urljoin(base_url + '/', segment_url)
            if not (absolute_url.endswith('.ts') or '/hl' in absolute_url.lower() or absolute_url.endswith('.m3u8')):
                return segment_url
            scheme = request.url.scheme
            host = request.url.hostname
            port = request.url.port or (443 if scheme == 'https' else 80)
            proxied_url = f"{scheme}://{host}:{port}/proxy?url={urllib.parse.quote(absolute_url)}"
            return proxied_url
        except ValueError:
            return segment_url
    return re.sub(r'^(?!#)\S+', replace_url, playlist_content, flags=re.MULTILINE)

@app.get("/proxy")
async def proxy(url: str, request: Request):
    client_ip = get_ip(request)
    cache_key = get_cache_key(client_ip, url)
    if not url:
        raise HTTPException(status_code=400, detail="No URL provided")
    session = requests.Session()
    headers = {
        "User-Agent": DEFAULT_USER_AGENT,
        'Accept-Encoding': 'identity',
        'Accept': '*/*',
        'Connection': 'keep-alive'
    }
    try:
        response = session.get(url, headers=headers, stream=True, timeout=10)
        if response.status_code == 200:
            content_type = response.headers.get('content-type', '').lower()
            if 'application/x-mpegURL' in content_type or '.m3u8' in url.lower():
                base_url = url.rsplit('/', 1)[0]
                playlist_content = response.content.decode('utf-8', errors='ignore')
                rewritten_playlist = rewrite_m3u8_urls(playlist_content, base_url, request)
                return StreamingResponse(
                    content=iter([rewritten_playlist.encode('utf-8')]),
                    media_type='application/x-mpegURL'
                )
            return StreamingResponse(response.iter_content(chunk_size=4096), media_type=content_type)
        else:
            raise HTTPException(status_code=response.status_code, detail="Erro ao acessar URL")
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))

@app.get("/")
def main_index():
    return {"message": "StreamFlix Proxy Online"}

# Iniciar app com leitura da PORT
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8080))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
