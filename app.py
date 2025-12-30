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
import json
from typing import Dict, Optional
import hashlib

# Configurar logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(name)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = FastAPI(title="Proxy de Streaming HLS")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Configurações
DEFAULT_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"

# Cache para armazenar tokens e sessões
class TokenManager:
    def __init__(self):
        self.tokens: Dict[str, Dict] = {}
        self.session_cache: Dict[str, requests.Session] = {}
    
    def get_session(self, key: str) -> requests.Session:
        if key not in self.session_cache:
            session = requests.Session()
            adapter = HTTPAdapter(
                pool_connections=10,
                pool_maxsize=100,
                max_retries=Retry(total=2, backoff_factor=0.5)
            )
            session.mount('http://', adapter)
            session.mount('https://', adapter)
            self.session_cache[key] = session
        return self.session_cache[key]
    
    def store_token(self, domain: str, token: str):
        self.tokens[domain] = {
            'token': token,
            'timestamp': time.time()
        }
    
    def get_token(self, domain: str) -> Optional[str]:
        if domain in self.tokens:
            return self.tokens[domain]['token']
        return None

token_manager = TokenManager()

def extract_domain(url: str) -> str:
    """Extrai o domínio de uma URL"""
    parsed = urlparse(url)
    return parsed.netloc

def extract_token_from_url(url: str) -> Optional[str]:
    """Extrai token da URL se existir"""
    try:
        parsed = urlparse(url)
        query_params = parse_qs(parsed.query)
        if 'token' in query_params:
            return query_params['token'][0]
    except:
        pass
    return None

def should_use_original_url(url: str) -> bool:
    """Determina se deve usar URL original (sem proxy) para alguns casos"""
    # Se já tem token na URL, provavelmente pode acessar diretamente
    parsed = urlparse(url)
    query_params = parse_qs(parsed.query)
    return 'token' in query_params

def get_referer_for_url(url: str) -> str:
    """Gera referer apropriado para a URL"""
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}/"

@app.api_route("/proxy", methods=["GET", "HEAD"])
async def proxy(url: str, request: Request):
    """Endpoint principal do proxy com suporte a HLS"""
    
    if not url:
        raise HTTPException(status_code=400, detail="URL não fornecida")
    
    try:
        decoded_url = urllib.parse.unquote(url)
    except:
        decoded_url = url
    
    logger.info(f"Proxy request para: {decoded_url}")
    
    # Verificar se é um arquivo TS
    is_ts_file = decoded_url.lower().endswith('.ts') or '/hls/' in decoded_url.lower()
    
    # Para arquivos TS, verificar se temos token
    if is_ts_file:
        domain = extract_domain(decoded_url)
        stored_token = token_manager.get_token(domain)
        
        # Se temos token armazenado, tentar adicionar à URL
        if stored_token:
            parsed = urlparse(decoded_url)
            query_dict = parse_qs(parsed.query)
            query_dict['token'] = stored_token
            new_query = urllib.parse.urlencode(query_dict, doseq=True)
            decoded_url = parsed._replace(query=new_query).geturl()
            logger.info(f"Adicionado token à URL TS: {domain}")
    
    # Preparar headers
    headers = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept": "*/*",
        "Accept-Encoding": "identity",
        "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
        "Connection": "keep-alive",
    }
    
    # Adicionar Referer para evitar bloqueios
    referer = get_referer_for_url(decoded_url)
    headers["Referer"] = referer
    
    # Obter sessão
    session_key = request.client.host if request.client else "default"
    session = token_manager.get_session(session_key)
    
    try:
        # Para HEAD requests
        if request.method == "HEAD":
            response = session.head(
                decoded_url,
                headers=headers,
                timeout=(3, 5),
                allow_redirects=True
            )
            
            response_headers = dict(response.headers)
            # Limpar headers problemáticos
            for header in ['Content-Encoding', 'Transfer-Encoding', 'Content-Length']:
                response_headers.pop(header, None)
            
            return Response(headers=response_headers)
        
        # Para GET requests
        response = session.get(
            decoded_url,
            headers=headers,
            stream=True,
            timeout=(3, 10),
            allow_redirects=True
        )
        
        # Armazenar token se encontrado na resposta
        if response.history:  # Houve redirecionamento
            for resp in response.history:
                final_url = resp.url
                token = extract_token_from_url(final_url)
                if token:
                    domain = extract_domain(final_url)
                    token_manager.store_token(domain, token)
                    logger.info(f"Token armazenado para domínio: {domain}")
        
        response.raise_for_status()
        
        # Verificar tipo de conteúdo
        content_type = response.headers.get('content-type', 'application/octet-stream')
        
        # Processar playlist m3u8
        is_m3u8 = ('.m3u8' in decoded_url.lower() or 
                  'application/x-mpegurl' in content_type.lower() or
                  'vnd.apple.mpegurl' in content_type.lower())
        
        if is_m3u8:
            return process_m3u8_playlist(response, decoded_url, request)
        
        # Para arquivos de mídia (TS, MP4, etc.)
        return stream_media_file(response, content_type)
        
    except requests.exceptions.HTTPError as e:
        status_code = e.response.status_code if e.response else 500
        
        # Tratamento especial para erro 403
        if status_code == 403:
            logger.warning(f"Acesso negado (403) para: {decoded_url}")
            
            # Para arquivos TS com erro 403, tentar estratégias alternativas
            if is_ts_file:
                return await handle_ts_403_error(decoded_url, request)
            
            raise HTTPException(
                status_code=403,
                detail="Acesso negado pelo servidor de origem"
            )
        
        logger.error(f"Erro HTTP {status_code} para {decoded_url}: {e}")
        raise HTTPException(status_code=status_code, detail=f"Erro HTTP: {str(e)}")
        
    except requests.exceptions.Timeout:
        logger.error(f"Timeout para {decoded_url}")
        raise HTTPException(status_code=504, detail="Timeout do servidor")
        
    except requests.exceptions.ConnectionError:
        logger.error(f"Erro de conexão para {decoded_url}")
        raise HTTPException(status_code=502, detail="Erro de conexão")
        
    except Exception as e:
        logger.error(f"Erro inesperado para {decoded_url}: {e}")
        raise HTTPException(status_code=500, detail=f"Erro interno: {str(e)}")

async def handle_ts_403_error(ts_url: str, request: Request) -> Response:
    """Lida com erro 403 em arquivos TS"""
    
    logger.info(f"Tentando estratégias alternativas para TS bloqueado: {ts_url}")
    
    # Estratégia 1: Tentar obter novo token via playlist m3u8
    try:
        # Extrair base URL do TS
        if '/hls/' in ts_url:
            base_parts = ts_url.split('/hls/')[0]
            m3u8_url = base_parts + '/live/paulotaguatinga/171652629170/'
            
            # Encontrar o m3u8 correto (precisaríamos do nome do arquivo)
            # Por enquanto, retornar erro com sugestão
            return JSONResponse(
                status_code=403,
                content={
                    "error": "TS bloqueado",
                    "message": "O servidor está bloqueando acesso direto aos segmentos TS",
                    "suggestion": "Tente atualizar a playlist m3u8 primeiro",
                    "ts_url": ts_url
                }
            )
    except Exception as e:
        logger.error(f"Erro ao processar estratégia TS: {e}")
    
    # Se nada funcionar, retornar erro 403
    raise HTTPException(
        status_code=403,
        detail="Segmento TS bloqueado pelo servidor. Tente recarregar a stream."
    )

def process_m3u8_playlist(response, original_url: str, request: Request) -> StreamingResponse:
    """Processa e reescreve playlist m3u8"""
    
    content = response.content.decode('utf-8', errors='ignore')
    base_url = original_url.rsplit('/', 1)[0] if '/' in original_url else original_url
    
    # Extrair token da URL original se existir
    parsed_original = urlparse(original_url)
    original_token = extract_token_from_url(original_url)
    
    if original_token:
        domain = extract_domain(original_url)
        token_manager.store_token(domain, original_token)
    
    # Reescrever URLs
    lines = content.split('\n')
    processed_lines = []
    
    for line in lines:
        line = line.strip()
        
        # Manter comentários e linhas vazias
        if not line or line.startswith('#'):
            processed_lines.append(line)
            continue
        
        # É uma URL
        if not line.startswith(('http://', 'https://')):
            # URL relativa
            absolute_url = urljoin(base_url + '/', line)
        else:
            absolute_url = line
        
        # Verificar se é URL de mídia
        is_media_url = any(ext in absolute_url.lower() for ext in ['.ts', '.m4s', '.mp4', '.aac'])
        
        if is_media_url:
            # Para URLs de mídia, tentar usar token se disponível
            parsed = urlparse(absolute_url)
            domain = extract_domain(absolute_url)
            stored_token = token_manager.get_token(domain)
            
            if stored_token and not extract_token_from_url(absolute_url):
                # Adicionar token à URL
                query_dict = parse_qs(parsed.query)
                query_dict['token'] = stored_token
                new_query = urllib.parse.urlencode(query_dict, doseq=True)
                absolute_url = parsed._replace(query=new_query).geturl()
            
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
            
            processed_lines.append(proxied_url)
        else:
            # Para outras URLs (outras playlists m3u8), também passar pelo proxy
            scheme = request.url.scheme
            host = request.url.hostname
            port = request.url.port
            
            if port and port not in [80, 443]:
                proxy_base = f"{scheme}://{host}:{port}"
            else:
                proxy_base = f"{scheme}://{host}"
            
            encoded_url = urllib.parse.quote(absolute_url, safe='')
            proxied_url = f"{proxy_base}/proxy?url={encoded_url}"
            processed_lines.append(proxied_url)
    
    rewritten_content = '\n'.join(processed_lines)
    
    return StreamingResponse(
        content=iter([rewritten_content.encode('utf-8')]),
        media_type='application/vnd.apple.mpegurl',
        headers={
            'Cache-Control': 'no-cache',
            'Access-Control-Allow-Origin': '*',
        }
    )

def stream_media_file(response, content_type: str) -> StreamingResponse:
    """Transmite arquivo de mídia"""
    
    def generate():
        chunk_size = 8192 * 8  # 64KB chunks
        
        try:
            for chunk in response.iter_content(chunk_size=chunk_size):
                if chunk:
                    yield chunk
        except Exception as e:
            logger.warning(f"Erro durante streaming: {e}")
        finally:
            response.close()
    
    headers = {
        'Content-Type': content_type,
        'Cache-Control': 'public, max-age=3600',
        'Access-Control-Allow-Origin': '*',
        'Accept-Ranges': 'bytes',
    }
    
    # Copiar headers relevantes
    for header in ['Content-Length', 'Content-Range', 'ETag']:
        if header in response.headers:
            headers[header] = response.headers[header]
    
    return StreamingResponse(
        content=generate(),
        media_type=content_type,
        headers=headers
    )

@app.get("/")
async def root():
    return {
        "status": "online",
        "service": "Proxy de Streaming HLS",
        "version": "2.0",
        "endpoints": {
            "proxy": "/proxy?url=URL_ENCODED",
            "health": "/health",
            "tokens": "/tokens (debug)"
        }
    }

@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "timestamp": time.time(),
        "tokens_stored": len(token_manager.tokens)
    }

@app.get("/tokens")
async def debug_tokens():
    """Endpoint de debug para ver tokens armazenados"""
    return {
        "tokens": token_manager.tokens,
        "session_count": len(token_manager.session_cache)
    }

@app.get("/clear-tokens")
async def clear_tokens():
    """Limpar cache de tokens"""
    token_manager.tokens.clear()
    return {"message": "Tokens limpos"}

if __name__ == "__main__":
    import uvicorn
    
    port = int(os.getenv("PORT", 8080))
    host = os.getenv("HOST", "0.0.0.0")
    
    uvicorn.run(
        "main:app",
        host=host,
        port=port,
        log_level="info",
        timeout_keep_alive=30
    )
