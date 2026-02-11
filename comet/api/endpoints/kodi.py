import base64
import binascii
from urllib.parse import quote, unquote, urlparse

import orjson
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError

from comet.core.models import ConfigModel, settings
from comet.services.kodi_pairing import (associate_setup_code_with_b64config,
                                         consume_b64config_for_setup_code,
                                         create_setup_code)
from comet.utils.cache import NO_CACHE_HEADERS

router = APIRouter()


class GenerateSetupCodeRequest(BaseModel):
    secret_string: str = ""


class AssociateManifestRequest(BaseModel):
    code: str = Field(min_length=6, max_length=16)
    manifest_url: str


def _extract_b64config_from_manifest_url(manifest_url: str):
    path_segments = [
        segment for segment in urlparse(manifest_url).path.split("/") if segment
    ]

    if not path_segments or path_segments[-1] != "manifest.json":
        raise ValueError("Invalid manifest URL format")

    if len(path_segments) == 1:
        return ""

    return unquote(path_segments[-2])


def _validate_b64config(b64config: str):
    try:
        try:
            decoded = base64.b64decode(b64config, validate=True)
        except binascii.Error:
            decoded = base64.urlsafe_b64decode(
                b64config + ("=" * (-len(b64config) % 4))
            )
        parsed = orjson.loads(decoded)
        ConfigModel(**parsed)
    except (
        ValidationError,
        binascii.Error,
        orjson.JSONDecodeError,
        TypeError,
        ValueError,
    ) as exc:
        raise ValueError("Invalid Comet configuration payload") from exc


def _base_url_from_request(request: Request):
    return settings.PUBLIC_BASE_URL or f"{request.url.scheme}://{request.url.netloc}"


@router.post(
    "/kodi/generate_setup_code",
    tags=["Kodi"],
    summary="Generate Kodi Setup Code",
    description="Generates a short-lived setup code used by Kodi to complete pairing.",
)
async def generate_setup_code(request: Request, payload: GenerateSetupCodeRequest):
    code, expires_in = await create_setup_code()
    base_url = _base_url_from_request(request)

    if payload.secret_string:
        encoded_secret = quote(payload.secret_string, safe="")
        configure_url = f"{base_url}/{encoded_secret}/configure?kodi_code={code}"
    else:
        configure_url = f"{base_url}/configure?kodi_code={code}"

    return JSONResponse(
        content={
            "code": code,
            "configure_url": configure_url,
            "expires_in": expires_in,
        },
        headers=NO_CACHE_HEADERS,
    )


@router.post(
    "/kodi/associate_manifest",
    tags=["Kodi"],
    summary="Associate Kodi Setup Code",
    description="Associates a generated setup code with a Comet manifest URL.",
)
async def associate_manifest(payload: AssociateManifestRequest):
    try:
        b64config = _extract_b64config_from_manifest_url(payload.manifest_url)
        if b64config:
            _validate_b64config(b64config)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    associated = await associate_setup_code_with_b64config(payload.code, b64config)
    if not associated:
        raise HTTPException(status_code=404, detail="Setup code not found or expired")

    return JSONResponse(content={"status": "success"}, headers=NO_CACHE_HEADERS)


@router.get(
    "/kodi/get_manifest/{code}",
    tags=["Kodi"],
    summary="Fetch Paired Kodi Configuration",
    description="Returns the Comet configuration for a setup code.",
)
async def get_manifest(code: str):
    b64config = await consume_b64config_for_setup_code(code)
    if b64config is None:
        raise HTTPException(
            status_code=404, detail="Manifest not ready or setup code expired"
        )

    return JSONResponse(
        content={"secret_string": b64config},
        headers=NO_CACHE_HEADERS,
    )
