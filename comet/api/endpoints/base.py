from fastapi import APIRouter
from fastapi.responses import RedirectResponse

router = APIRouter()


@router.get(
    "/",
    tags=["General"],
    summary="Root Redirect",
    description="Redirects to the configuration page.",
)
async def root():
    return RedirectResponse("/configure")


@router.get(
    "/health",
    tags=["General"],
    summary="Health Check",
    description="Returns the health status of the application.",
)
async def health():
    return {"status": "ok"}
