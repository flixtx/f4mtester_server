from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
import urllib.parse
import requests
import os
import re
import time
import logging
from urllib.parse import urljoin, urlparse, parse_qs
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from typing import Dict, Optional, Tuple
import asyncio
from contextlib import asynccontextmanager

# Configurar logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(name)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Variáveis globais para cache
TOKEN_CACHE: Dict[str, Dict] = {}
SESSION_CACHE: Dict[str, requests.Session] = {}
ACTIVE_STREAMS: Dict[str, asyncio.Task] = {}

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("Starting up proxy server...")
    yield
    # Shutdown
    logger.info("Shutting down proxy server...")
    for session in SESSION_CACHE.values():
        session.close()
    for task in ACTIVE_STREAMS.values():
        task.cancel()

app = FastAPI(
    title="Proxy de Streaming HLS/MP4",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Configurações
DEFAULT_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"

class StreamManager:
    """Gerencia streams ativos para evitar múltiplas conexões para o mesmo recurso"""
    
    def __init__(self):
        self.active_streams: Dict[str, asyncio.Event] = {}
        self.stream_data: Dict[str, bytes] = {}
    
    async def get_or_create_stream(self, url: str, session: requests.Session, headers: Dict) -> bytes:
        """Obtém ou cria um stream para a URL"""
        stream_id = hashlib.md5(url.encode()).hexdigest()
        
        if stream_id in self.stream_data:
            return self.stream_data[stream_id]
        
        # Criar novo stream
        event = asyncio.Event()
        self.active_streams[stream_id] = event
        
        try:
            response = session.get(
                url,
                headers=headers,
                stream=True,
                timeout=(10, 60)  # 10s connect, 60s read
            )
            
            # Baixar em chunks e armazenar
            chunks = []
            for chunk in response.iter_content(chunk_size=8192 * 16):  # 128KB chunks
                if chunk:
                    chunks.append(chunk)
            
            data = b''.join(chunks)
            self.stream_data[stream_id] = data
            event.set()
            
            return data
            
        except Exception as e:
            logger.error(f"Erro ao baixar stream {url}: {e}")
            raise
        finally:
            self.active_streams.pop(stream_id, None)

stream_manager = StreamManager()

def create_robust_session() -> requests.Session:
    """Cria uma sessão HTTP robusta para grandes transferências"""
    session = requests.Session()
    
    # Configurar retry apenas para erros específicos
    retry_strategy = Retry(
        total=2,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "HEAD"],
        respect_retry_after_header=True
    )
    
    adapter = HTTPAdapter(
        max_retries=retry_strategy,
        pool_connections=50,
        pool_maxsize=50,
        pool_block=False
    )
    
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    
    return session

def get_session() -> requests.Session:
    """Obtém uma sessão HTTP (reutilizável)"""
    if 'default' not in SESSION_CACHE:
        SESSION_CACHE['default'] = create_robust_session()
    return SESSION_CACHE['default']

def is_large_file(url: str, content_type: str) -> bool:
    """Determina se é um arquivo grande baseado na URL e content-type"""
    large_extensions = ['.mp4', '.mkv', '.avi', '.mov', '.wmv']
    url_lower = url.lower()
    
    return (any(ext in url_lower for ext in large_extensions) or
            'video/' in content_type.lower() or
            'application/octet-stream' in content_type.lower())

def generate_safe_stream(response, chunk_size: int = 8192 * 8):
    """Gerador seguro para streaming que lida com conexões interrompidas"""
    try:
        bytes_streamed = 0
        for chunk in response.iter_content(chunk_size=chunk_size):
            if chunk:
                bytes_streamed += len(chunk)
                yield chunk
                
        logger.info(f"Streaming concluído: {bytes_streamed} bytes transferidos")
        
    except requests.exceptions.ChunkedEncodingError as e:
        logger.warning(f"Streaming interrompido (ChunkedEncodingError): {e}")
        # Não relançar a exceção, apenas parar o streaming
        return
    except Exception as e:
        logger.error(f"Erro durante streaming: {e}")
        return
    finally:
        try:
            response.close()
        except:
            pass

@app.api_route("/proxy", methods=["GET", "HEAD"])
async def proxy(url: str, request: Request, range_header: Optional[str] = None):
    """
    Proxy com suporte para:
    - HEAD requests
    - Range requests (para streaming de vídeo)
    - Large files
    - Redirecionamentos com tokens
    """
    
    if not url:
        raise HTTPException(status_code=400, detail="URL não fornecida")
    
    try:
        decoded_url = urllib.parse.unquote(url)
    except:
        decoded_url = url
    
    logger.info(f"Proxy request: {decoded_url[:100]}...")
    
    # Preparar headers
    headers = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept": "*/*",
        "Accept-Encoding": "identity",  # Importante para streaming
        "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
        "Connection": "keep-alive",
    }
    
    # Adicionar Referer
    parsed = urlparse(decoded_url)
    headers["Referer"] = f"{parsed.scheme}://{parsed.netloc}/"
    
    # Copiar Range header do cliente (para streaming de vídeo)
    if range_header:
        headers["Range"] = range_header
    elif "range" in request.headers:
        headers["Range"] = request.headers["range"]
    
    # Obter sessão
    session = get_session()
    
    try:
        # Para HEAD requests
        if request.method == "HEAD":
            response = session.head(
                decoded_url,
                headers=headers,
                timeout=(5, 10),
                allow_redirects=True
            )
            
            # Construir headers de resposta
            response_headers = {}
            for key, value in response.headers.items():
                if key.lower() not in ['content-encoding', 'transfer-encoding', 'connection']:
                    response_headers[key] = value
            
            # Garantir que temos Content-Length
            if 'content-length' not in response_headers:
                response_headers['Content-Length'] = '0'
            
            return Response(headers=response_headers)
        
        # Para GET requests
        response = session.get(
            decoded_url,
            headers=headers,
            stream=True,
            timeout=(10, 60),  # Timeouts maiores para arquivos grandes
            allow_redirects=True
        )
        
        response.raise_for_status()
        
        # Determinar tipo de conteúdo
        content_type = response.headers.get('content-type', 'application/octet-stream')
        
        # Verificar se é um arquivo grande
        is_large = is_large_file(decoded_url, content_type)
        
        if is_large:
            logger.info(f"Processando arquivo grande: {decoded_url}")
            return handle_large_file(response, decoded_url, content_type)
        
        # Processar playlist m3u8
        is_m3u8 = ('.m3u8' in decoded_url.lower() or 
                  'application/x-mpegurl' in content_type.lower() or
                  'vnd.apple.mpegurl' in content_type.lower())
        
        if is_m3u8:
            return process_m3u8_playlist(response, decoded_url, request)
        
        # Para outros arquivos (TS, etc.)
        return stream_small_file(response, content_type)
        
    except requests.exceptions.Timeout as e:
        logger.error(f"Timeout para {decoded_url}: {e}")
        raise HTTPException(status_code=504, detail="Timeout do servidor de origem")
        
    except requests.exceptions.ConnectionError as e:
        logger.error(f"Connection error para {decoded_url}: {e}")
        raise HTTPException(status_code=502, detail="Erro de conexão com o servidor")
        
    except requests.exceptions.HTTPError as e:
        status_code = e.response.status_code if e.response else 500
        logger.error(f"HTTP error {status_code} para {decoded_url}: {e}")
        
        if status_code == 403:
            return JSONResponse(
                status_code=403,
                content={
                    "error": "Acesso negado",
                    "message": "O servidor bloqueou o acesso a este recurso",
                    "url": decoded_url
                }
            )
        
        raise HTTPException(status_code=status_code, detail=f"Erro HTTP: {str(e)}")
        
    except Exception as e:
        logger.error(f"Unexpected error para {decoded_url}: {e}")
        raise HTTPException(status_code=500, detail=f"Erro interno: {str(e)}")

def handle_large_file(response, url: str, content_type: str) -> StreamingResponse:
    """Lida com streaming de arquivos grandes (MP4, etc.)"""
    
    # Preparar headers de resposta
    headers = {
        'Content-Type': content_type,
        'Accept-Ranges': 'bytes',
        'Cache-Control': 'public, max-age=86400',  # Cache de 1 dia para arquivos grandes
        'Access-Control-Allow-Origin': '*',
        'Access-Control-Expose-Headers': 'Content-Length, Content-Range',
    }
    
    # Copiar headers importantes da resposta original
    for header in ['Content-Length', 'Content-Range', 'ETag', 'Last-Modified']:
        if header in response.headers:
            headers[header] = response.headers[header]
    
    # Usar chunk size maior para arquivos grandes
    chunk_size = 8192 * 32  # 256KB chunks
    
    return StreamingResponse(
        content=generate_safe_stream(response, chunk_size),
        media_type=content_type,
        headers=headers
    )

def stream_small_file(response, content_type: str) -> StreamingResponse:
    """Streaming para arquivos pequenos"""
    
    headers = {
        'Content-Type': content_type,
        'Cache-Control': 'no-cache',
        'Access-Control-Allow-Origin': '*',
    }
    
    # Copiar Content-Length se disponível
    if 'Content-Length' in response.headers:
        headers['Content-Length'] = response.headers['Content-Length']
    
    return StreamingResponse(
        content=generate_safe_stream(response),
        media_type=content_type,
        headers=headers
    )

def process_m3u8_playlist(response, original_url: str, request: Request) -> StreamingResponse:
    """Processa playlist m3u8"""
    
    content = response.content.decode('utf-8', errors='ignore')
    base_url = original_url.rsplit('/', 1)[0] if '/' in original_url else original_url
    
    # Extrair token se existir
    parsed = urlparse(original_url)
    query_params = parse_qs(parsed.query)
    
    if 'token' in query_params:
        token = query_params['token'][0]
        domain = parsed.netloc
        TOKEN_CACHE[domain] = {
            'token': token,
            'timestamp': time.time()
        }
    
    # Reescrever URLs
    lines = content.split('\n')
    processed_lines = []
    
    for line in lines:
        line = line.strip()
        
        if not line or line.startswith('#'):
            processed_lines.append(line)
            continue
        
        # É uma URL
        if not line.startswith(('http://', 'https://')):
            absolute_url = urljoin(base_url + '/', line)
        else:
            absolute_url = line
        
        # Verificar se é arquivo de mídia
        is_media = any(ext in absolute_url.lower() for ext in ['.ts', '.m4s', '.mp4', '.aac', '.vtt'])
        
        # Para arquivos TS, tentar adicionar token cacheado
        if '.ts' in absolute_url.lower():
            parsed_media = urlparse(absolute_url)
            domain = parsed_media.netloc
            
            if domain in TOKEN_CACHE:
                # Adicionar token à URL
                media_query = parse_qs(parsed_media.query)
                if 'token' not in media_query:
                    media_query['token'] = TOKEN_CACHE[domain]['token']
                    new_query = urllib.parse.urlencode(media_query, doseq=True)
                    absolute_url = parsed_media._replace(query=new_query).geturl()
        
        # Criar URL proxy
        scheme = request.url.scheme
        host = request.url.hostname
        port = request.url.port
        
        proxy_base = f"{scheme}://{host}"
        if port and port not in [80, 443]:
            proxy_base = f"{scheme}://{host}:{port}"
        
        encoded_url = urllib.parse.quote(absolute_url, safe='')
        proxied_url = f"{proxy_base}/proxy?url={encoded_url}"
        
        processed_lines.append(proxied_url)
    
    rewritten_content = '\n'.join(processed_lines)
    
    return StreamingResponse(
        content=iter([rewritten_content.encode('utf-8')]),
        media_type='application/vnd.apple.mpegurl',
        headers={
            'Cache-Control': 'no-cache, max-age=0',
            'Access-Control-Allow-Origin': '*',
        }
    )

@app.get("/")
async def root():
    return {
        "status": "online",
        "service": "Proxy de Streaming Otimizado",
        "version": "3.0",
        "features": [
            "Suporte a arquivos grandes (MP4, etc.)",
            "Streaming com chunking seguro",
            "Cache de tokens HLS",
            "Range requests para vídeo",
            "Timeout otimizado"
        ],
        "endpoints": {
            "proxy": "GET /proxy?url=URL_ENCODED",
            "health": "GET /health",
            "stats": "GET /stats"
        }
    }

@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "timestamp": time.time(),
        "cache": {
            "tokens": len(TOKEN_CACHE),
            "sessions": len(SESSION_CACHE)
        }
    }

@app.get("/stats")
async def stats():
    """Estatísticas do servidor"""
    import psutil
    import multiprocessing
    
    memory = psutil.virtual_memory()
    
    return {
        "memory": {
            "total": memory.total,
            "available": memory.available,
            "percent": memory.percent
        },
        "cpu_count": multiprocessing.cpu_count(),
        "cache": {
            "tokens": len(TOKEN_CACHE),
            "sessions": len(SESSION_CACHE)
        }
    }

@app.get("/clear-cache")
async def clear_cache():
    """Limpar caches"""
    TOKEN_CACHE.clear()
    for session in SESSION_CACHE.values():
        session.close()
    SESSION_CACHE.clear()
    
    return {"message": "Cache limpo"}

# Middleware para log de requests
@app.middleware("http")
async def log_requests(request: Request, call_next):
    start_time = time.time()
    
    response = await call_next(request)
    
    process_time = time.time() - start_time
    logger.info(f"{request.method} {request.url.path} - {response.status_code} - {process_time:.2f}s")
    
    return response

if __name__ == "__main__":
    import uvicorn
    
    # Configurações do servidor
    port = int(os.getenv("PORT", 8080))
    host = os.getenv("HOST", "0.0.0.0")
    
    # Configurações otimizadas para streaming
    uvicorn_config = {
        "host": host,
        "port": port,
        "log_level": "info",
        "timeout_keep_alive": 300,  # 5 minutos para streaming longo
        "limit_concurrency": 100,
        "limit_max_requests": 10000,
        "http": "httptools",  # Mais rápido que h11
    }
    
    logger.info(f"Starting server on {host}:{port}")
    uvicorn.run("main:app", **uvicorn_config)
