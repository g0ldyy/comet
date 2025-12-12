from fastapi import APIRouter

from comet.core.metrics import prom_response

router = APIRouter()


@router.get("/metrics")
async def metrics_endpoint():
    return prom_response()
