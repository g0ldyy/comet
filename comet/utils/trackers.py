import urllib.parse
import asyncio
import aiohttp
from parallel_utils.thread import Monitor, create_thread, synchronized
from aiohttp_client_cache import CachedSession
from aiohttp_client_cache.backends.filesystem import FileBackend
from threading import Semaphore

from comet.utils.logger import logger

cached_trackers = set({
    "udp://tracker-udp.gbitt.info:80/announce",
    "udp://tracker.0x7c0.com:6969/announce",
    "udp://opentracker.io:6969/announce",
    "udp://leet-tracker.moe:1337/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://tracker.tiny-vps.com:6969/announce",
    "udp://tracker.leechers-paradise.org:6969/announce",
    "udp://tracker.pomf.se:80/announce",
    "udp://9.rarbg.me:2710/announce",
    "http://tracker.gbitt.info:80/announce",
    "udp://tracker.bittor.pw:1337/announce",
    "udp://open.free-tracker.ga:6969/announce",
    "udp://open.stealth.si:80/announce",
    "udp://retracker01-msk-virt.corbina.net:80/announce",
    "udp://tracker.openbittorrent.com:80/announce",
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://isk.richardsw.club:6969/announce",
    "https://tracker.gbitt.info:443/announce",
    "udp://tracker.coppersurfer.tk:6969/announce",
    "udp://oh.fuuuuuck.com:6969/announce",
    "udp://ipv4.tracker.harry.lu:80/announce",
    "udp://open.demonii.com:1337/announce",
    "https://tracker.tamersunion.org:443/announce",
    "https://tracker.renfei.net:443/announce",
    "udp://open.tracker.cl:1337/announce",
    "udp://tracker.internetwarriors.net:1337/announce",
    "udp://exodus.desync.com:6969/announce",
    "udp://tracker.dump.cl:6969/announce",
})

monitor = Monitor()
refresh_semaphore = Semaphore(1)
is_cache_being_refreshed = False

# Configuração do cache em disco
cache = FileBackend(
    cache_name='newtrackon_cache', # Nome do diretório ou arquivo de cache
    use_temp=False, 
    expire_after=60*10,
    autoclose=True,
)

async def post_newtrackon_trackers(input_trackers) -> None:
   
    existing_sources = get_cached_trackers()
    new_trackers = input_trackers.difference(existing_sources)
    if len(new_trackers) == 0:
        return
    
    
    cached_trackers.update(new_trackers)
    
    # Codifica cada tracker individualmente
    encoded_trackers = '\n'.join(
        urllib.parse.quote(tracker, safe='') for tracker in list(new_trackers)
    )
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

async def refresh_trackers_cache():
    """Atualiza a lista de trackers a partir da API remota."""
    global is_cache_being_refreshed
    
    # Tenta adquirir o semáforo sem bloquear
    if not refresh_semaphore.acquire(blocking=False):
        return  # Já está em execução, sai imediatamente
    
    try:
        # Marca o estado como atualizando
        with monitor.synchronized("cache_state_update"):
            is_cache_being_refreshed = True

        async with CachedSession(cache=cache) as session:
            response = await session.get("https://newtrackon.com/api/all")
            response.raise_for_status()
            response_text = await response.text()

            # Processa os novos trackers
            new_trackers = [tracker.strip() for tracker in response_text.split("\n") if tracker.strip()]
            
            # Atualiza a lista em cache
            with monitor.synchronized("cached_trackers_update"):
                cached_trackers.update(new_trackers)
                
    except Exception as e:
        logger.warning(f"Erro ao atualizar cache de trackers: {e}")
    finally:
        # Libera o semáforo e reseta o estado
        refresh_semaphore.release()
        with monitor.synchronized("cache_state_update"):
            is_cache_being_refreshed = False

@synchronized(max_threads=1)  # Garante uma única atualização por vez
def start_cache_refresh():
    """Inicia a atualização do cache em uma thread assíncrona."""
    try:
        asyncio.run(refresh_trackers_cache())
    except Exception as e:
        logger.warning(f"Erro na atualização assíncrona: {e}")

def get_cached_trackers():
    """
    Retorna uma cópia dos trackers armazenados em cache.
    
    NOTA: A lista retornada pode estar desatualizada até que uma atualização completa seja concluída.
    A atualização é disparada automaticamente em segundo plano, se necessário.
    """
    global is_cache_being_refreshed
    
    # Verifica se o cache precisa ser atualizado
    with monitor.synchronized("cache_state_check"):
        if not is_cache_being_refreshed:
            is_cache_being_refreshed = True
            create_thread(start_cache_refresh)

    # Retorna uma cópia segura do cache atual
    with monitor.synchronized("cached_trackers_read"):
        return list(cached_trackers)