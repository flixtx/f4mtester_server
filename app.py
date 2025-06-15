from fastapi import FastAPI, HTTPException, Request
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
    level=logging.DEBUG,  # Define o nível de log como DEBUG
    format='%(asctime)s - %(levelname)s - %(message)s'
)

app = FastAPI()
DEFAULT_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
IP_CACHE_TS = {}
IP_CACHE_MP4 = {}
AGENT_OF_CHAOS = {}

def rewrite_m3u8_urls(playlist_content: str, base_url: str, request: Request) -> str:
    def replace_url(match):
        segment_url = match.group(0).strip()
        # Ignorar linhas que começam com #, estão vazias, ou não são URLs válidas
        if segment_url.startswith('#') or not segment_url or segment_url == '/':
            return segment_url
        # Resolver URL absoluta
        try:
            absolute_url = urljoin(base_url + '/', segment_url)
            # Verificar se a URL é válida e corresponde a um segmento esperado
            if not (absolute_url.endswith('.ts') or '/hl' in absolute_url.lower() or absolute_url.endswith('.m3u8')):
                logging.debug(f"[HLS Proxy] Ignorando URL inválida no m3u8: {absolute_url}")
                return segment_url
            scheme = request.url.scheme
            host = request.url.hostname
            port = request.url.port or (443 if scheme == 'https' else 80)
            proxied_url = f"{scheme}://{host}:{port}/proxy?url={urllib.parse.quote(absolute_url)}"
            return proxied_url
        except ValueError as e:
            logging.debug(f"[HLS Proxy] Erro ao resolver URL {segment_url}: {e}")
            return segment_url

    # Reescrever apenas linhas que não começam com # e não estão vazias
    return re.sub(r'^(?!#)\S+', replace_url, playlist_content, flags=re.MULTILINE)

async def stream_response(response, client_ip: str, url: str, headers: dict, sess: requests.Session):
    def generate_chunks(response):
        mode_ts = False
        bytes_read = 0
        try:
            for chunk in response.iter_content(chunk_size=4095):
                if chunk:
                    bytes_read += len(chunk)
                    if '.mp4' in response.url.lower():
                        mode_ts = False
                        if client_ip not in IP_CACHE_MP4:
                            IP_CACHE_MP4[client_ip] = []
                        IP_CACHE_MP4[client_ip].append(chunk)
                        if len(IP_CACHE_MP4[client_ip]) > 20:
                            IP_CACHE_MP4[client_ip].pop(0)
                    elif '.ts' in response.url.lower() or '/hl' in response.url.lower():
                        mode_ts = True
                        if client_ip not in IP_CACHE_TS:
                            IP_CACHE_TS[client_ip] = []
                        IP_CACHE_TS[client_ip].append(chunk)
                        if len(IP_CACHE_TS[client_ip]) > 20:
                            IP_CACHE_TS[client_ip].pop(0)
                    yield chunk
        except (IncompleteRead, ConnectionError) as e:
            logging.debug(f"[HLS Proxy] Erro ao processar chunks (bytes lidos: {bytes_read}): {e}")
            if mode_ts and client_ip in IP_CACHE_TS and IP_CACHE_TS[client_ip]:
                for chunk in IP_CACHE_TS[client_ip][-5:]:
                    yield chunk
            elif not mode_ts and client_ip in IP_CACHE_MP4 and IP_CACHE_MP4[client_ip]:
                for chunk in IP_CACHE_MP4[client_ip][-5:]:
                    yield chunk
        except Exception as e:
            logging.debug(f"[HLS Proxy] Erro inesperado ao processar chunks: {e}")
        finally:
            sess.close()

    iterator = generate_chunks(response)
    while True:
        try:
            chunk = await to_thread.run_sync(lambda: next(iterator, None))
            if chunk is None:
                break
            yield chunk
        except StopIteration:
            break

@app.get("/proxy")
async def proxy(url: str, request: Request):
    client_ip = request.client.host
    if not url:
        raise HTTPException(status_code=400, detail="No URL provided")

    session = requests.Session()
    default_headers = {
        "User-Agent": DEFAULT_USER_AGENT,
        'Accept-Encoding': 'identity',
        'Accept': '*/*',
        'Connection': 'keep-alive'
    }
    session.headers.update(default_headers)
    max_retries = 35
    attempts = 0
    tried_without_range = False

    while attempts < max_retries:
        # Validação inicial da URL
        if not ('.m3u8' in url.lower() or '.mp4' in url.lower() or '.ts' in url.lower() or '/hl' in url.lower()):
            logging.debug(f"[HLS Proxy] URL inválida: {url}")
            raise HTTPException(status_code=400, detail="Nenhuma URL compatível com o proxy")
        
        logging.debug(f'Tentativa {attempts}')
        logging.debug(f'Acessando: {url}')

        try:
            range_header = request.headers.get('Range')
            if '.mp4' in url.lower() and range_header and not tried_without_range:
                default_headers['Range'] = range_header
            else:
                default_headers.pop('Range', None)

            if '.mp4' in url.lower():
                headers = default_headers
                response = session.get(url, headers=headers, allow_redirects=True, stream=True, timeout=60)
            elif ('.ts' in url.lower() or '/hl' in url.lower() or '.m3u8' in url.lower() or '.mp4' in url.lower()) and client_ip in AGENT_OF_CHAOS:
                headers = default_headers
                custom_header = {'User-Agent': AGENT_OF_CHAOS.get(client_ip, DEFAULT_USER_AGENT)}
                headers.update(custom_header)
                response = requests.get(url, headers=headers, allow_redirects=True, stream=True, timeout=60)
            else:
                headers = default_headers
                response = session.get(url, allow_redirects=True, stream=True, timeout=60)

            logging.debug(f'Acessando com headers: {headers}')

            if response.status_code in (200, 206):
                try:
                    if client_ip in AGENT_OF_CHAOS:
                        del AGENT_OF_CHAOS[client_ip]
                except:
                    pass
                logging.debug(f'acesso ok codigo: {response.status_code}')
                content_type = response.headers.get('content-type', '').lower()

                if 'application/vnd.apple.mpegurl' in content_type or '.m3u8' in url.lower():
                    base_url = url.rsplit('/', 1)[0]
                    playlist_content = response.content.decode('utf-8', errors='ignore')
                    rewritten_playlist = rewrite_m3u8_urls(playlist_content, base_url, request)
                    return StreamingResponse(
                        content=iter([rewritten_playlist.encode('utf-8')]),
                        media_type='application/vnd.apple.mpegurl'
                    )

                if '.mp4' in url.lower() and client_ip in IP_CACHE_MP4:
                    IP_CACHE_MP4[client_ip] = []
                elif '.ts' in url.lower() and client_ip in IP_CACHE_TS:
                    IP_CACHE_TS[client_ip] = []
                elif '/hl' in url.lower() and client_ip in IP_CACHE_TS:
                    IP_CACHE_TS[client_ip] = []

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
                    content=stream_response(response, client_ip, url, headers, session),
                    media_type=media_type,
                    headers=response_headers,
                    status_code=status_code
                )

            elif response.status_code == 416:
                #print(f"[HLS Proxy] Erro 416: Range Not Satisfiable para {url}")
                if range_header and not tried_without_range:
                    #print(f"[HLS Proxy] Tentando novamente sem header Range")
                    tried_without_range = True
                    continue
                else:
                    raise HTTPException(status_code=416, detail="Range Not Satisfiable")

            elif response.status_code == 404 and ('.ts' in url.lower() or '/hl' in url.lower()):
                logging.debug(f'codigo: {response.status_code}')
                #print(f"[HLS Proxy] Segmento HLS não encontrado: {url}")
                if client_ip in IP_CACHE_TS and IP_CACHE_TS[client_ip]:
                    last_chunks = IP_CACHE_TS[client_ip][-5:]
                    media_type = 'video/mp2t'
                    return StreamingResponse(
                        content=iter(last_chunks),
                        media_type=media_type,
                        headers={'Content-Type': media_type}
                    )
                attempts += 1
                AGENT_OF_CHAOS[client_ip] = binascii.b2a_hex(os.urandom(20))[:32]
                time.sleep(2)

            else:
                logging.debug(f'codigo: {response.status_code}')
                AGENT_OF_CHAOS[client_ip] = binascii.b2a_hex(os.urandom(20))[:32]
                if not '.m3u8' in url.lower():
                    if '.mp4' in url.lower():
                        if client_ip in IP_CACHE_MP4 and IP_CACHE_MP4[client_ip]:
                            last_chunks = IP_CACHE_MP4[client_ip][-5:]
                            media_type = 'video/mp4' if '.mp4' in url.lower() else 'video/mp2t'
                            return StreamingResponse(
                                content=iter(last_chunks),
                                media_type=media_type,
                                headers={'Content-Type': media_type}
                            )
                    if '.ts' in url.lower() or '/hl' in url.lower():
                        if client_ip in IP_CACHE_TS and IP_CACHE_TS[client_ip]:
                            last_chunks = IP_CACHE_TS[client_ip][-5:]
                            media_type = 'video/mp4' if '.mp4' in url.lower() else 'video/mp2t'
                            return StreamingResponse(
                                content=iter(last_chunks),
                                media_type=media_type,
                                headers={'Content-Type': media_type}
                            )

                attempts += 1
                time.sleep(2)

        except RequestException as e:
            logging.debug(f'Erro desconhecido {e}')
            AGENT_OF_CHAOS[client_ip] = binascii.b2a_hex(os.urandom(20))[:32]
            if not '.m3u8' in url.lower():
                if '.mp4' in url.lower():
                    if client_ip in IP_CACHE_MP4 and IP_CACHE_MP4[client_ip]:
                        last_chunks = IP_CACHE_MP4[client_ip][-5:]
                        media_type = 'video/mp4' if '.mp4' in url.lower() else 'video/mp2t'
                        return StreamingResponse(
                            content=iter(last_chunks),
                            media_type=media_type,
                            headers={'Content-Type': media_type}
                        )
                if '.ts' in url.lower() or '/hl' in url.lower():
                    if client_ip in IP_CACHE_TS and IP_CACHE_TS[client_ip]:
                        last_chunks = IP_CACHE_TS[client_ip][-5:]
                        media_type = 'video/mp4' if '.mp4' in url.lower() else 'video/mp2t'
                        return StreamingResponse(
                            content=iter(last_chunks),
                            media_type=media_type,
                            headers={'Content-Type': media_type}
                        )
            attempts += 1
            time.sleep(2)

    raise HTTPException(status_code=502, detail="Falha ao conectar após múltiplas tentativas")

@app.get("/check")
async def check(url: str, request: Request):
    session = requests.Session()
    if '.m3u8' in url:
        default_headers = {
        "User-Agent": DEFAULT_USER_AGENT,
        'Accept-Encoding': 'identity',
        'Accept': '*/*',
        'Connection': 'keep-alive'
        }
        response = session.get(url, headers=default_headers, allow_redirects=True, stream=True, timeout=15)
        if response.status_code != 200:
            default_headers.update({'User-Agent': binascii.b2a_hex(os.urandom(20))[:32]})
            response = session.get(url, headers=default_headers, allow_redirects=True, stream=True, timeout=15)
        try:
            return {'code': response.status_code}
        except:
            return {'code': 'error'}
    else:
        return {'message': 'only m3u8 links'}


@app.get("/")
def main_index():
    return {"message": "F4MTESTER PROXY"}
