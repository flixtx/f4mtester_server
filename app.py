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
from requests.exceptions import ConnectionError, RequestException, Timeout, ChunkedEncodingError
from urllib3.exceptions import IncompleteRead, ProtocolError
import time
import logging
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import hashlib
from typing import Optional

# Configurar logging mais detalhado
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - [%(name)s] - %(message)s'
)
logger = logging.getLogger(__name__)

app = FastAPI(title="Proxy de Streaming")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["GET", "HEAD", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

DEFAULT_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"

# Caches para melhor performance
CACHE = {
    "m3u8": {},  # Cache de playlists
    "sessions": {},  # Cache de sessões por IP
    "tokens": {},  # Cache de tokens de autenticação
}

def create_session_with_retry(max_retries: int = 3) -> requests.Session:
    """Cria uma sessão HTTP com mecanismo de retry"""
    session = requests.Session()
    
    retry_strategy = Retry(
        total=max_retries,
        backoff_factor=0.5,
        status_forcelist=[403, 408, 429, 500, 502, 503, 504],
        allowed_methods=["GET", "HEAD"],
        respect_retry_after_header=True
    )
    
    adapter = HTTPAdapter(
        max_retries=retry_strategy,
        pool_connections=100,
        pool_maxsize=100
    )
    
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    
    return session

def get_session_for_ip(ip: str) -> requests.Session:
    """Obtém ou cria uma sessão para um IP específico"""
    if ip not in CACHE["sessions"]:
        CACHE["sessions"][ip] = create_session_with_retry()
    return CACHE["sessions"][ip]

def get_ip(request: Request) -> str:
    """Extrai o IP real do cliente"""
    forwarded_for = request.headers.get("x-forwarded-for")
    real_ip = request.headers.get("x-real-ip")
    
    if forwarded_for:
        ip = forwarded_for.split(",")[0].strip()
    elif real_ip:
        ip = real_ip
    else:
        ip = request.client.host if request.client else "0.0.0.0"
    
    return ip

def extract_auth_headers_from_url(url: str) -> dict:
    """Extrai informações de autenticação da URL se disponível"""
    headers = {}
    try:
        # Tenta extrair token da URL
        parsed = urllib.parse.urlparse(url)
        query_params = urllib.parse.parse_qs(parsed.query)
        
        if 'token' in query_params:
            headers['Referer'] = f"{parsed.scheme}://{parsed.netloc}/"
            # Adicionar cookies se necessário
            headers['Cookie'] = f"token={query_params['token'][0]}"
            
    except Exception:
        pass
    
    return headers

def rewrite_m3u8_urls(playlist_content: str, base_url: str, request: Request) -> str:
    """Reescreve URLs em arquivos m3u8 para passar pelo proxy"""
    
    def replace_url(match):
        segment_url = match.group(0).strip()
        
        # Ignorar linhas de comentário ou vazias
        if (segment_url.startswith('#') or 
            not segment_url or 
            segment_url == '/' or
            '://' in segment_url and not segment_url.startswith('http')):
            return segment_url
        
        try:
            # Converter URL relativa para absoluta
            if not segment_url.startswith(('http://', 'https://')):
                absolute_url = urljoin(base_url + '/', segment_url)
            else:
                absolute_url = segment_url
            
            # Filtrar apenas URLs de mídia
            if not any(ext in absolute_url.lower() for ext in ['.ts', '.m3u8', '.mp4', '.m4s', '.aac']):
                return segment_url
            
            # Criar URL do proxy
            scheme = request.url.scheme
            host = request.url.hostname
            port = request.url.port
            
            if port and port not in [80, 443]:
                proxy_base = f"{scheme}://{host}:{port}"
            else:
                proxy_base = f"{scheme}://{host}"
            
            encoded_url = urllib.parse.quote(absolute_url, safe='')
            proxied_url = f"{proxy_base}/proxy?url={encoded_url}"
            
            return proxied_url
            
        except Exception as e:
            logger.warning(f"Erro ao reescrever URL {segment_url}: {e}")
            return segment_url
    
    # Processar cada linha do playlist
    lines = playlist_content.split('\n')
    processed_lines = []
    
    for line in lines:
        if line.strip() and not line.startswith('#'):
            processed_line = re.sub(r'^[^#\s]+', replace_url, line)
            processed_lines.append(processed_line)
        else:
            processed_lines.append(line)
    
    return '\n'.join(processed_lines)

def should_cache_response(url: str, content_type: str) -> bool:
    """Determina se a resposta deve ser cacheada"""
    # Cache apenas playlists m3u8 (não arquivos grandes de mídia)
    return ('.m3u8' in url.lower() or 
            'application/x-mpegurl' in content_type.lower() or
            'vnd.apple.mpegurl' in content_type.lower())

@app.api_route("/proxy", methods=["GET", "HEAD"])
async def proxy(url: str, request: Request):
    """Endpoint principal do proxy"""
    
    if not url:
        raise HTTPException(status_code=400, detail="URL não fornecida")
    
    try:
        decoded_url = urllib.parse.unquote(url)
    except Exception:
        decoded_url = url
    
    client_ip = get_ip(request)
    logger.info(f"Proxy request from {client_ip} to {decoded_url}")
    
    # Obter sessão para este IP
    session = get_session_for_ip(client_ip)
    
    # Headers padrão
    headers = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept": "*/*",
        "Accept-Encoding": "identity",  # Desabilitar compressão para streaming
        "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
        "Connection": "keep-alive",
        "Cache-Control": "no-cache",
    }
    
    # Adicionar headers de autenticação se disponíveis
    auth_headers = extract_auth_headers_from_url(decoded_url)
    headers.update(auth_headers)
    
    # Copiar alguns headers do cliente
    for header in ['Range', 'If-Modified-Since', 'If-None-Match']:
        if header in request.headers:
            headers[header] = request.headers[header]
    
    try:
        # Para HEAD requests, apenas obter headers
        if request.method == "HEAD":
            response = session.head(
                decoded_url, 
                headers=headers, 
                timeout=(3, 5),
                allow_redirects=True
            )
            
            response_headers = dict(response.headers)
            # Remover headers problemáticos
            for header in ['Content-Encoding', 'Transfer-Encoding', 'Content-Length']:
                response_headers.pop(header, None)
            
            return Response(headers=response_headers)
        
        # Para GET requests
        response = session.get(
            decoded_url,
            headers=headers,
            stream=True,
            timeout=(3, 30),  # 3s connect, 30s read
            allow_redirects=True
        )
        
        response.raise_for_status()
        
        # Determinar content-type
        content_type = response.headers.get('content-type', 'application/octet-stream')
        
        # Verificar se é um playlist m3u8
        is_m3u8 = ('.m3u8' in decoded_url.lower() or 
                   'application/x-mpegurl' in content_type.lower() or
                   'vnd.apple.mpegurl' in content_type.lower())
        
        if is_m3u8:
            # Processar playlist m3u8
            base_url = decoded_url.rsplit('/', 1)[0] if '/' in decoded_url else decoded_url
            playlist_content = response.content.decode('utf-8', errors='ignore')
            
            # Reescrever URLs
            rewritten_playlist = rewrite_m3u8_urls(playlist_content, base_url, request)
            
            return StreamingResponse(
                content=iter([rewritten_playlist.encode('utf-8')]),
                media_type='application/vnd.apple.mpegurl',
                headers={
                    'Cache-Control': 'no-cache, max-age=0',
                    'Access-Control-Allow-Origin': '*',
                }
            )
        
        # Para arquivos de mídia (TS, MP4, etc.)
        def generate_chunks():
            """Gerador para transmitir chunks de dados"""
            chunk_size = 8192 * 4  # 32KB chunks para melhor performance
            
            try:
                for chunk in response.iter_content(chunk_size=chunk_size):
                    if chunk:
                        yield chunk
            except (ChunkedEncodingError, IncompleteRead, ProtocolError) as e:
                logger.warning(f"Erro durante streaming: {e}")
                # Não levantar exceção, apenas parar o streaming
                return
            except Exception as e:
                logger.error(f"Erro inesperado no streaming: {e}")
                return
            finally:
                response.close()
        
        # Headers para resposta de streaming
        response_headers = {
            'Content-Type': content_type,
            'Cache-Control': 'no-cache',
            'Access-Control-Allow-Origin': '*',
            'Accept-Ranges': 'bytes',
        }
        
        # Copiar headers relevantes da resposta original
        for header in ['Content-Length', 'Content-Range', 'ETag', 'Last-Modified']:
            if header in response.headers:
                response_headers[header] = response.headers[header]
        
        return StreamingResponse(
            content=generate_chunks(),
            media_type=content_type,
            headers=response_headers
        )
        
    except Timeout as e:
        logger.error(f"Timeout para {decoded_url}: {e}")
        raise HTTPException(status_code=504, detail="Timeout do servidor de origem")
        
    except ConnectionError as e:
        logger.error(f"Erro de conexão para {decoded_url}: {e}")
        raise HTTPException(status_code=502, detail="Erro de conexão com o servidor")
        
    except requests.exceptions.HTTPError as e:
        status_code = e.response.status_code if e.response else 500
        logger.error(f"Erro HTTP {status_code} para {decoded_url}")
        raise HTTPException(status_code=status_code, detail=f"Erro HTTP: {e}")
        
    except Exception as e:
        logger.error(f"Erro inesperado para {decoded_url}: {e}")
        raise HTTPException(status_code=500, detail=f"Erro interno: {str(e)}")

@app.get("/")
async def root():
    """Página inicial"""
    return {
        "status": "online",
        "service": "Proxy de Streaming",
        "endpoints": {
            "proxy": "/proxy?url=URL_ENCODED",
            "health": "/health"
        }
    }

@app.get("/health")
async def health_check():
    """Endpoint de health check"""
    return {
        "status": "healthy",
        "timestamp": time.time(),
        "cache_stats": {
            "sessions": len(CACHE["sessions"]),
            "m3u8_entries": len(CACHE["m3u8"]),
        }
    }

@app.on_event("shutdown")
async def shutdown_event():
    """Limpar recursos ao desligar"""
    for session in CACHE["sessions"].values():
        session.close()
    logger.info("Sessões HTTP fechadas")

# Iniciar app
if __name__ == "__main__":
    import uvicorn
    
    # Configurações
    port = int(os.getenv("PORT", 8080))
    host = os.getenv("HOST", "0.0.0.0")
    
    # Configurar uvicorn
    config = uvicorn.Config(
        "main:app",
        host=host,
        port=port,
        log_level="info",
        timeout_keep_alive=30,
        limit_concurrency=100,
        limit_max_requests=1000
    )
    
    server = uvicorn.Server(config)
    server.run()
