import secrets
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Response, status

from comet.core.models import settings
from comet.observability import CONTENT_TYPE_LATEST, metrics, render_metrics

router = APIRouter()


@router.get(
    settings.PROMETHEUS_PATH,
    include_in_schema=False,
    response_class=Response,
)
async def prometheus_metrics(
    authorization: Annotated[str | None, Header()] = None,
):
    expected_token = metrics.auth_token
    if expected_token is not None:
        expected_header = f"Bearer {expected_token}"
        if authorization is None or not secrets.compare_digest(
            authorization.encode(), expected_header.encode()
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid metrics credentials",
                headers={"WWW-Authenticate": "Bearer"},
            )

    return Response(
        content=render_metrics(),
        headers={"Content-Type": CONTENT_TYPE_LATEST},
    )
