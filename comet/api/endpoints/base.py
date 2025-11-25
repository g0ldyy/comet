from fastapi import APIRouter
from fastapi.responses import RedirectResponse

router = APIRouter()


@router.get("/")
async def root():
    return RedirectResponse("/configure")


@router.get("/health")
async def health():
    return {"status": "ok"}
