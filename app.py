# salve como: main.py
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from starlette.middleware.base import BaseHTTPMiddleware
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
from uuid import uuid4

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

class RequestIDMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request.state.request_id = str(uuid4())
        response = await call_next(request)
        return response

app.add_middleware(RequestIDMiddleware)

DEFAULT_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/130.0.0.0 Safari/537.36"

IP_CACHE_TS = {}
IP_CACHE_MP4 = {}
AGENT_OF_CHAOS = {}

def get_cache_key(request: Request, url: str) -> str:
    return f"{request.state.request_id}:{url}"

def rewrite_m3u8_urls(playlist_content: str, base_url: str, request: Request) -> str:
    def replace_url(match):
        segment_url = match.group(0).strip()
        if segment_url.startswith('#') or not segment_url:
            return segment_url
        try:
            absolute_url = urljoin(base_url + '/', segment_url)
            scheme = request.url.scheme
            host = request.url.hostname
            port = request.url.port or (443 if scheme == 'https' else 80)
            return f"{scheme}://{host}:{port}/proxy?url={urllib.parse.quote(absolute_url)}"
        except ValueError:
            return segment_url

    return re.sub(r'^(?!#)\S+', replace_url, playlist_content, flags=re.MULTILINE)

async def stream_response(response, cache_key: str, headers: dict, sess: requests.Session):
    def generate_chunks():
        mode_ts = '.ts' in response.url.lower() or '/hl' in response.url.lower()
        try:
            for chunk in response.iter_content(chunk_size=4096):
                if chunk:
                    if mode_ts:
                        IP_CACHE_TS.setdefault(cache_key, []).append(chunk)
                        if len(IP_CACHE_TS[cache_key]) > 20:
                            IP_CACHE_TS[cache_key].pop(0)
                    elif '.mp4' in response.url.lower():
                        IP_CACHE_MP4.setdefault(cache_key, []).append(chunk)
                        if len(IP_CACHE_MP4[cache_key]) > 20:
                            IP_CACHE_MP4[cache_key].pop(0)
                    yield chunk
        except Exception:
            cache = IP_CACHE_TS if mode_ts else IP_CACHE_MP4
            if cache_key in cache and cache[cache_key]:
                for chunk in cache[cache_key][-5:]:
                    yield chunk
        finally:
            sess.close()

    gen = generate_chunks()
    while True:
        chunk = await to_thread.run_sync(lambda: next(gen, None))
        if chunk is None:
            break
        yield chunk

@app.get("/proxy")
async def proxy(url: str, request: Request):
    if not url:
        raise HTTPException(status_code=400, detail="No URL provided")

    cache_key = get_cache_key(request, url)
    session = requests.Session()
    headers = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept-Encoding": "identity",
        "Accept": "*/*",
        "Connection": "keep-alive"
    }

    max_attempts = 7
    tried_without_range = False
    for attempt in range(max_attempts):
        if not ('.m3u8' in url.lower() or '.mp4' in url.lower() or '.ts' in url.lower() or '/hl' in url.lower()):
            logging.debug(f"[HLS Proxy] URL inválida: {url}")
            raise HTTPException(status_code=400, detail="Nenhuma URL compatível com o proxy")        
        try:
            range_header = request.headers.get('Range')
            if '.mp4' in url.lower() and range_header and not tried_without_range:
                headers['Range'] = range_header
            else:
                headers.pop('Range', None)
            
            if '.mp4' in url.lower():
                response = session.get(url, headers=headers, stream=True, timeout=60)
            else:
                headers["User-Agent"] = AGENT_OF_CHAOS.get(cache_key, DEFAULT_USER_AGENT)
                response = session.get(url, headers=headers, stream=True, timeout=60)
            logging.debug('AGENT {0}'.format(headers['User-Agent']))
            
            if response.status_code in (200, 206):
                content_type = response.headers.get('content-type', '').lower()
                if 'application/vnd.apple.mpegurl' in content_type or '.m3u8' in url:
                    playlist = response.content.decode('utf-8', errors='ignore')
                    rewritten = rewrite_m3u8_urls(playlist, url.rsplit('/', 1)[0], request)
                    return StreamingResponse(iter([rewritten.encode('utf-8')]), media_type='application/vnd.apple.mpegurl')

                response_headers = {
                    key: value for key, value in response.headers.items()
                    if key.lower() in ('content-type', 'accept-ranges', 'content-range')
                }

                media_type = (
                    'video/mp4' if '.mp4' in url.lower()
                    else 'video/mp2t' if '.ts' in url.lower() or '/hl' in url
                    else response_headers.get('content-type', 'application/octet-stream')
                )

                status_code = 206 if response.status_code == 206 else 200
                if response.status_code == 206 and 'Content-Range' in response.headers:
                    response_headers['Content-Range'] = response.headers.get('Content-Range', '') 
               
                return StreamingResponse(
                    stream_response(response, cache_key, headers, session),
                    media_type=media_type,
                    headers=response_headers,
                    status_code=status_code
                )
            elif response.status_code == 416:
                if range_header and not tried_without_range:
                    tried_without_range = True
                    continue
                else:
                    raise HTTPException(status_code=416, detail="Range Not Satisfiable")
    
            elif response.status_code == 404 and ('.ts' in url.lower() or '/hl' in url.lower()):
                logging.debug('ERROR 404')
                AGENT_OF_CHAOS[cache_key] = binascii.b2a_hex(os.urandom(20))[:32]
                cache = IP_CACHE_TS.get(cache_key, [])
                if cache:
                    logging.debug('USANDO CACHE')
                    return StreamingResponse(iter(cache[-5:]), media_type='video/mp2t')
                time.sleep(2)

            else:
                AGENT_OF_CHAOS[cache_key] = binascii.b2a_hex(os.urandom(20))[:32]
                time.sleep(2)

        except RequestException:
            AGENT_OF_CHAOS[cache_key] = binascii.b2a_hex(os.urandom(20))[:32]
            time.sleep(2)

    raise HTTPException(status_code=502, detail="Não foi possível conectar ao servidor de origem.")

@app.get("/check")
async def check(url: str):
    session = requests.Session()
    if '.m3u8' in url or 'get.php' in url:
        headers = {"User-Agent": DEFAULT_USER_AGENT}
        try:
            response = session.get(url, headers=headers, timeout=15)
            return {'code': response.status_code}
        except:
            return {'code': 0}
    return {'message': 'only m3u8 links'}

@app.get("/")
def index():
    return {"message": "F4MTESTER PROXY"}
