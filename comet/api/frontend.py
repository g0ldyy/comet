"""Production asset delivery for the Comet web application."""

from functools import lru_cache
from html import escape
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

_PACKAGE_DIST = Path(__file__).resolve().parents[1] / "frontend_dist"
_SOURCE_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"
_INDEX_MARKER = "<!--COMET_CUSTOM_HEADER-->"
_DOCUMENT_META_MARKER = "<!--COMET_DOCUMENT_META-->"
_DOCUMENT_TITLE_MARKER = "<!--COMET_DOCUMENT_TITLE-->"
_PUBLIC_TITLE_SUFFIX = " — Built to find it first"
_PUBLIC_TITLE = f"Comet{_PUBLIC_TITLE_SUFFIX}"
_PUBLIC_DESCRIPTION = "Stremio's fastest torrent/debrid/usenet search add-on."
_SOCIAL_IMAGE_PATH = "/brand/comet-social-card.png"
_PRIVATE_ROBOTS = "noindex, nofollow, noarchive"
ADMIN_CSP = (
    "default-src 'self'; base-uri 'self'; connect-src 'self'; "
    "font-src 'self'; form-action 'self'; frame-ancestors 'self'; "
    "img-src 'self' data:; object-src 'none'; script-src 'self'; style-src 'self'"
)
PUBLIC_CUSTOM_CSP = (
    "default-src 'self' data: blob: https: http:; base-uri 'self'; "
    "frame-ancestors 'self'; object-src 'none'; style-src 'self' 'unsafe-inline'"
)


class ImmutableStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        if response.status_code == 200:
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


def frontend_dist() -> Path | None:
    for path in (_PACKAGE_DIST, _SOURCE_DIST):
        if (path / "index.html").is_file():
            return path
    return None


def install_frontend_assets(app: FastAPI) -> bool:
    root = frontend_dist()
    if root is None:
        return False

    app.mount(
        "/assets",
        ImmutableStaticFiles(directory=root / "assets"),
        name="frontend-assets",
    )
    app.mount("/brand", StaticFiles(directory=root / "brand"), name="frontend-brand")

    async def manifest():
        return FileResponse(
            root / "manifest.webmanifest",
            media_type="application/manifest+json",
            headers={"Cache-Control": "no-cache"},
        )

    app.add_api_route(
        "/manifest.webmanifest",
        manifest,
        methods=["GET"],
        include_in_schema=False,
    )

    async def robots():
        return PlainTextResponse(
            "User-agent: *\nAllow: /configure\nDisallow: /admin\nDisallow: /api/\n",
            headers={"Cache-Control": "public, max-age=3600"},
        )

    app.add_api_route(
        "/robots.txt",
        robots,
        methods=["GET"],
        include_in_schema=False,
    )
    for path in ("/admin", "/admin/", "/admin/{path:path}"):
        app.add_api_route(
            path,
            admin_frontend_response,
            methods=["GET"],
            include_in_schema=False,
        )
    return True


async def admin_frontend_response(_request: Request) -> HTMLResponse:
    response = frontend_index_response(public=False)
    response.headers["Cache-Control"] = "private, no-store"
    return response


def frontend_index_response(
    *,
    public: bool,
    public_base_url: str | None = None,
    indexable: bool = False,
    custom_header_html: str | None = None,
    root: Path | None = None,
) -> HTMLResponse:
    dist = root or frontend_dist()
    if dist is None:
        raise RuntimeError("frontend assets were not built")

    customization = custom_header_html if public and custom_header_html else ""
    metadata = (
        _public_document_metadata(public_base_url, indexable=indexable)
        if public
        else f'<meta name="robots" content="{_PRIVATE_ROBOTS}" />'
    )
    title_suffix = _PUBLIC_TITLE_SUFFIX if public else " Administration"
    content = (
        _index_template(dist)
        .replace(_INDEX_MARKER, customization)
        .replace(_DOCUMENT_META_MARKER, metadata)
        .replace(_DOCUMENT_TITLE_MARKER, title_suffix)
    )
    headers = {
        "Cache-Control": "no-cache",
        "Content-Security-Policy": PUBLIC_CUSTOM_CSP if public else ADMIN_CSP,
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
    }
    if not indexable:
        headers["X-Robots-Tag"] = _PRIVATE_ROBOTS
    return HTMLResponse(
        content,
        headers=headers,
    )


def _public_document_metadata(
    public_base_url: str | None,
    *,
    indexable: bool,
) -> str:
    canonical_url = escape(f"{public_base_url.rstrip('/')}/configure", quote=True)
    image_url = escape(
        f"{public_base_url.rstrip('/')}{_SOCIAL_IMAGE_PATH}",
        quote=True,
    )
    title = escape(_PUBLIC_TITLE, quote=True)
    description = escape(_PUBLIC_DESCRIPTION, quote=True)
    robots = (
        "" if indexable else f'<meta name="robots" content="{_PRIVATE_ROBOTS}" />\n    '
    )
    return (
        f'<meta name="description" content="{description}" />\n'
        f'    {robots}<link rel="canonical" href="{canonical_url}" />\n'
        f'    <meta property="og:type" content="website" />\n'
        f'    <meta property="og:site_name" content="Comet" />\n'
        f'    <meta property="og:locale" content="en_US" />\n'
        f'    <meta property="og:title" content="{title}" />\n'
        f'    <meta property="og:description" content="{description}" />\n'
        f'    <meta property="og:url" content="{canonical_url}" />\n'
        f'    <meta property="og:image" content="{image_url}" />\n'
        f'    <meta property="og:image:type" content="image/png" />\n'
        f'    <meta property="og:image:width" content="1200" />\n'
        f'    <meta property="og:image:height" content="630" />\n'
        f'    <meta property="og:image:alt" content="{title}" />\n'
        f'    <meta name="twitter:card" content="summary_large_image" />\n'
        f'    <meta name="twitter:title" content="{title}" />\n'
        f'    <meta name="twitter:description" content="{description}" />\n'
        f'    <meta name="twitter:image" content="{image_url}" />\n'
        f'    <meta name="twitter:image:alt" content="{title}" />'
    )


@lru_cache(maxsize=2)
def _index_template(root: Path) -> str:
    return (root / "index.html").read_text(encoding="utf-8")
