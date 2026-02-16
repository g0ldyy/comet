from fastapi import APIRouter, Request

from comet.core.config_validation import config_check
from comet.core.logger import logger
from comet.debrid.manager import get_debrid_credentials
from comet.services.debrid_account_scraper import trigger_account_snapshot_sync
from comet.services.status_video import build_status_video_response
from comet.utils.http_client import http_client_manager
from comet.utils.network import get_client_ip
from comet.utils.parsing import parse_optional_int

router = APIRouter()


@router.get(
    "/{b64config}/debrid-sync/{service_index}",
    tags=["Stremio"],
    summary="Debrid Account Sync Trigger",
    description="Triggers a debrid account snapshot sync for the selected service.",
)
async def debrid_sync(
    request: Request,
    b64config: str,
    service_index: str,
):
    config = config_check(b64config)
    parsed_service_index = parse_optional_int(service_index)

    debrid_service, debrid_api_key = get_debrid_credentials(
        config, parsed_service_index
    )
    session = await http_client_manager.get_session()
    ip = get_client_ip(request)

    sync_started = await trigger_account_snapshot_sync(
        session, debrid_service, debrid_api_key, ip
    )

    if sync_started:
        logger.log(
            "SCRAPER",
            f"{debrid_service}: Manual account sync triggered via debrid-sync endpoint",
        )
        video_code = "DEBRID_SYNC_TRIGGERED"
    else:
        logger.log(
            "SCRAPER",
            f"{debrid_service}: Manual account sync already running",
        )
        video_code = "DEBRID_SYNC_ALREADY_RUNNING"

    return build_status_video_response([video_code], default_key=video_code)
