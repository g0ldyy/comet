import urllib.parse
import asyncio
import aiohttp
from parallel_utils.thread import Monitor, create_thread, synchronized
import aiohttp_client_cache 
from aiohttp_client_cache import CachedSession
from aiohttp_client_cache.backends.filesystem import FileBackend

from comet.utils.logger import logger

trackers = [
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
]

monitor = Monitor()
monitor.is_updating = False # Inicializa a flag de atualização

# Configuração do cache em disco
cache = FileBackend(
    cache_name='newtrackon_cache', # Nome do diretório ou arquivo de cache
    use_temp=False, 
    expire_after=60*10,
    autoclose=True,
)

async def post_newtrackon_trackers(new_trackers: list) -> None:
    """
    Envia uma lista de novos trackers para o NewTrackon.
    
    Args:
        new_trackers (list): Lista de URLs de trackers que não estão no NewTrackon.
    """
    if not new_trackers:
        return

    # Codifica cada tracker individualmente e junta sem separadores
    encoded_trackers = ''.join(urllib.parse.quote(tracker, safe='') for tracker in new_trackers)
    payload = f"new_trackers={encoded_trackers}"
    headers = {"Content-Type": "application/x-www-form-urlencoded"}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://newtrackon.com/api/add ",
                data=payload,
                headers=headers
            ) as response:
                if response.status == 200:
                    logger.info(f"✅ {len(new_trackers)} novo(s) tracker(s) adicionado(s) ao NewTrackon.")
                else:
                    logger.warning(f"❌ Falha ao adicionar trackers ao NewTrackon. Código: {response.status}")
    except Exception as e:
        logger.warning(f"⚠️ Erro ao enviar trackers para NewTrackon: {e}")

async def download_newtrackon():
    try:
        async with CachedSession(cache=cache) as session:
            response = await session.get("https://newtrackon.com/api/all")
            response_text = await response.text()
            other_trackers = [tracker.strip() for tracker in response_text.split("\n") if tracker.strip()]

            # Atualiza a lista de forma segura
            with monitor.synchronized("trackers_update"):
                trackers.extend(other_trackers)
            print("Trackers atualizados")
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
        return list(trackers)

async def download_best_trackers():
    try:
        async with aiohttp.ClientSession() as session:
            response = await session.get(
                "https://raw.githubusercontent.com/ngosang/trackerslist/master/trackers_best.txt"
            )
            response = await response.text()

            other_trackers = [tracker for tracker in response.split("\n") if tracker]
            trackers.extend(other_trackers)
    except Exception as e:
        logger.warning(f"Failed to download best trackers: {e}")
