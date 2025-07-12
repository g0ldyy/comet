import urllib.parse
import asyncio
import aiohttp
from parallel_utils.thread import Monitor, create_thread, synchronized
from aiohttp_client_cache import CachedSession
from aiohttp_client_cache.backends.filesystem import FileBackend

from comet.utils.logger import logger

trackers = set()

monitor = Monitor()
monitor.is_updating = False # Inicializa a flag de atualização

# Configuração do cache em disco
cache = FileBackend(
    cache_name='newtrackon_cache', # Nome do diretório ou arquivo de cache
    use_temp=False, 
    expire_after=60*10,
    autoclose=True,
)

async def post_newtrackon_trackers(input_trackers) -> None:
   
    existing_sources = get_trackers()
    new_trackers = input_trackers.difference(existing_sources)
    if len(new_trackers) == 0:
        return
    
    
    trackers.update(new_trackers)
    
    # Codifica cada tracker individualmente e junta sem separadores
    encoded_trackers = ''.join(urllib.parse.quote(tracker, safe='') for tracker in list(new_trackers))
    payload = f"new_trackers={encoded_trackers}"
    headers = {"Content-Type": "application/x-www-form-urlencoded"}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://newtrackon.com/api/add",
                data=payload,
                headers=headers
            ) as response:
                if response.status == 204:
                    logger.info(f"✅ {len(new_trackers)} Trackers added to the queue NewTrackon.")
                else:
                    logger.warning(f"❌ Falha ao adicionar trackers ao NewTrackon. Código: {response.status}")
    except Exception as e:
        logger.warning(f"⚠️ Erro ao enviar trackers para NewTrackon: {e}")

async def download_newtrackon():
    try:
        async with CachedSession(cache=cache) as session:
            response = await session.get("https://newtrackon.com/api/all")
            response_text = await response.text()
            
            # Atualiza a lista de forma segura
            with monitor.synchronized("trackers_update"):
                other_trackers = [tracker.strip() for tracker in response_text.split("\n") if tracker.strip()]               
                trackers.update(other_trackers)
    except Exception as e:
        logger.warning(f"Erro ao baixar trackers: {e}")
        
@synchronized(max_threads=1) # Garante que apenas uma thread execute esta função por vez
def run_async_in_thread():
    try:
        asyncio.run(download_newtrackon())
    finally:
        # Reseta a flag após a conclusão (sucesso ou erro)
        with monitor.synchronized("update_flag"):
            monitor.is_updating = False

def get_trackers():
    should_update = False
    # Verifica se há atualização em andamento (de forma segura)
    with monitor.synchronized("update_flag"):
        if not monitor.is_updating:
            monitor.is_updating = True
            should_update = True
    
    # Dispara a atualização em background (se necessário)
    if should_update:
        create_thread(run_async_in_thread)
    
    # Retorna a lista atual
    with monitor.synchronized("trackers_update"):
        return trackers
