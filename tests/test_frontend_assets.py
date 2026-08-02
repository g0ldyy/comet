from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from comet.api import frontend


def _dist(tmp_path: Path) -> Path:
    (tmp_path / "assets").mkdir()
    (tmp_path / "brand").mkdir()
    (tmp_path / "assets" / "app-abc123.js").write_text("export {}", encoding="utf-8")
    (tmp_path / "brand" / "favicon.svg").write_text("<svg/>", encoding="utf-8")
    (tmp_path / "brand" / "comet-social-card.png").write_bytes(b"social-card")
    (tmp_path / "manifest.webmanifest").write_text("{}", encoding="utf-8")
    (tmp_path / "index.html").write_text(
        "<html><head><!--COMET_DOCUMENT_META--><title>Comet<!--COMET_DOCUMENT_TITLE--></title>"
        '</head><body><template id="comet-custom-header"><!--COMET_CUSTOM_HEADER--></template>'
        "<main></main></body></html>",
        encoding="utf-8",
    )
    return tmp_path


def test_hashed_assets_are_immutable(monkeypatch, tmp_path):
    root = _dist(tmp_path)
    monkeypatch.setattr(frontend, "frontend_dist", lambda: root)
    app = FastAPI()

    assert frontend.install_frontend_assets(app)

    client = TestClient(app)
    response = client.get("/assets/app-abc123.js")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert "immutable" not in client.get("/assets/missing.js").headers.get(
        "cache-control", ""
    )
    assert client.get("/brand/comet-social-card.png").content == b"social-card"


def test_asset_mounts_do_not_capture_application_routes(monkeypatch, tmp_path):
    root = _dist(tmp_path)
    monkeypatch.setattr(frontend, "frontend_dist", lambda: root)
    app = FastAPI()

    @app.get("/api/example")
    async def example():
        return {"ok": True}

    frontend.install_frontend_assets(app)
    client = TestClient(app)

    assert client.get("/api/example").json() == {"ok": True}
    assert client.get("/unknown").status_code == 404
    robots = client.get("/robots.txt")
    assert robots.status_code == 200
    assert "Disallow: /admin" in robots.text
    for path in ("/admin", "/admin/system", "/admin/cometnet"):
        response = client.get(path)
        assert response.status_code == 200
        assert "<main></main>" in response.text
        assert "<title>Comet Administration</title>" in response.text
        assert 'content="noindex, nofollow, noarchive"' in response.text
        assert response.headers["cache-control"] == "private, no-store"
        assert response.headers["x-robots-tag"] == "noindex, nofollow, noarchive"


def test_public_index_injects_trusted_customization(tmp_path):
    root = _dist(tmp_path)

    response = frontend.frontend_index_response(
        public=True,
        public_base_url="https://comet.example",
        indexable=True,
        custom_header_html="<aside>Operator notice</aside>",
        root=root,
    )
    content = response.body.decode()

    assert "<aside>Operator notice</aside>" in content
    assert (
        '<template id="comet-custom-header"><aside>Operator notice</aside></template>'
        in content
    )
    assert (
        "style-src 'self' 'unsafe-inline'"
        in response.headers["content-security-policy"]
    )
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["content-security-policy"] == frontend.PUBLIC_CUSTOM_CSP
    assert "x-robots-tag" not in response.headers
    assert "<title>Comet — Built to find it first</title>" in content
    assert '<link rel="canonical" href="https://comet.example/configure" />' in content
    assert (
        '<meta property="og:image" '
        'content="https://comet.example/brand/comet-social-card.png" />' in content
    )
    assert '<meta name="twitter:card" content="summary_large_image" />' in content


def test_configured_url_is_private_but_shares_the_public_canonical_page(tmp_path):
    response = frontend.frontend_index_response(
        public=True,
        public_base_url="https://comet.example",
        root=_dist(tmp_path),
    )
    content = response.body.decode()

    assert response.headers["x-robots-tag"] == "noindex, nofollow, noarchive"
    assert '<meta name="robots" content="noindex, nofollow, noarchive" />' in content
    assert (
        '<meta property="og:url" content="https://comet.example/configure" />'
        in content
    )


def test_admin_index_excludes_public_customization_and_uses_strict_csp(tmp_path):
    root = _dist(tmp_path)

    response = frontend.frontend_index_response(
        public=False,
        custom_header_html="<script>unexpected()</script>",
        root=root,
    )

    assert "unexpected" not in response.body.decode()
    assert response.headers["content-security-policy"] == frontend.ADMIN_CSP
