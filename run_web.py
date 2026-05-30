"""PingCloud web server — static site + ranking API.

Lightweight standalone server that serves the frontend and ranking JSON data
without requiring DB / proxy / config.settings modules.
"""

import argparse
import asyncio
import gzip
import mimetypes
import ssl
from pathlib import Path

from aiohttp import web

from build_i18n import build as build_i18n
from config import DEFAULTS, load_config_file

_STATIC_DIR = Path(__file__).parent / "web" / "static"

# MIME types worth gzip-compressing (JSON, JS, CSS, SVG)
_GZIP_TYPES = {"application/json", "text/javascript", "text/css", "image/svg+xml"}

# Minimum body size to compress (small files gain nothing from gzip overhead)
_GZIP_MIN_SIZE = 256


async def index_handler(request: web.Request) -> web.FileResponse:
    """Serve English (default) index page at /."""
    resp = web.FileResponse(_STATIC_DIR / "index.en.html")
    # HTML must not be cached by CDN — __DATA_VERSION changes require fresh HTML
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


async def zh_index_handler(request: web.Request) -> web.FileResponse:
    """Serve Chinese index page at /zh/."""
    resp = web.FileResponse(_STATIC_DIR / "index.zh.html")
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


async def zh_redirect_handler(request: web.Request) -> web.StreamResponse:
    """Redirect /zh to /zh/ for consistent URL structure."""
    return web.HTTPMovedPermanently(location="/zh/")


async def sitemap_handler(request: web.Request) -> web.FileResponse:
    """Serve sitemap.xml at /sitemap.xml for SEO crawlers."""
    resp = web.FileResponse(_STATIC_DIR / "sitemap.xml")
    resp.headers["Cache-Control"] = "public, max-age=3600"
    return resp


async def robots_handler(request: web.Request) -> web.FileResponse:
    """Serve robots.txt at /robots.txt for SEO crawlers."""
    resp = web.FileResponse(_STATIC_DIR / "robots.txt")
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


# Static assets that rarely change — cache 1 year (cache-busted via ?v= query string)
_LONG_CACHE_PATHS = {"/static/map.png", "/static/images/pingcloud.png"}
_LONG_CACHE_MAX_AGE = 31536000  # 365 days


@web.middleware
async def _cache_middleware(request: web.Request, handler):
    resp = await handler(request)
    # All static assets use long cache; cache-busting via ?v= query string in HTML
    if request.path.startswith("/static/"):
        max_age = _LONG_CACHE_MAX_AGE if request.path in _LONG_CACHE_PATHS else 86400
        resp.headers["Cache-Control"] = f"public, max-age={max_age}"
    return resp


def _get_content_type(resp: web.StreamResponse) -> str:
    """Extract base Content-Type from a response, handling FileResponse."""
    ct = resp.headers.get("Content-Type", "")
    if ct:
        return ct.split(";")[0].strip()
    # FileResponse sets Content-Type later in prepare(); infer from path
    if isinstance(resp, web.FileResponse) and resp._path:
        guessed, _ = mimetypes.guess_type(str(resp._path))
        if guessed:
            return guessed
    return ""


async def _read_body(resp: web.StreamResponse) -> bytes:
    """Read the full response body, handling both Response and FileResponse."""
    if isinstance(resp, web.FileResponse):
        return Path(resp._path).read_bytes()
    return await resp.read()


@web.middleware
async def _gzip_middleware(request: web.Request, handler):
    resp = await handler(request)
    # Only compress if client accepts gzip
    if "gzip" not in request.headers.get("Accept-Encoding", ""):
        return resp
    # Determine content type (FileResponse needs special handling)
    base_ct = _get_content_type(resp)
    if base_ct not in _GZIP_TYPES:
        return resp
    # Read body, skip tiny responses
    try:
        body = await _read_body(resp)
    except Exception:
        return resp  # cannot read body — serve uncompressed
    if len(body) < _GZIP_MIN_SIZE:
        return resp
    # Gzip-encode and replace response
    gz_body = gzip.compress(body, compresslevel=6)
    gz_resp = web.Response(body=gz_body, status=resp.status, reason=resp.reason)
    for k, v in resp.headers.items():
        if k.lower() in ("content-length", "content-encoding", "content-type"):
            continue
        gz_resp.headers[k] = v
    # FileResponse may have wrong/empty Content-Type in headers; use the inferred one
    gz_resp.headers["Content-Type"] = base_ct
    gz_resp.headers["Content-Encoding"] = "gzip"
    gz_resp.headers["Content-Length"] = str(len(gz_body))
    gz_resp.headers["Vary"] = "Accept-Encoding"
    return gz_resp


def create_app() -> web.Application:
    # Auto-build pre-rendered i18n HTML on startup
    build_i18n()

    app = web.Application(middlewares=[_gzip_middleware, _cache_middleware])
    app.router.add_get("/", index_handler)
    app.router.add_get("/zh", zh_redirect_handler)
    app.router.add_get("/zh/", zh_index_handler)
    app.router.add_get("/sitemap.xml", sitemap_handler)
    app.router.add_get("/robots.txt", robots_handler)
    app.router.add_static("/static", _STATIC_DIR, name="static")
    return app


def _build_ssl_context(certfile: str, keyfile: str) -> ssl.SSLContext:
    """Build an SSL context from the given certificate and key files."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(certfile=certfile, keyfile=keyfile)
    return ctx


async def _run_dual(app: web.Application, http_port: int, https_port: int,
                    ssl_context: ssl.SSLContext):
    """Run the same app on both HTTP and HTTPS ports simultaneously."""
    runner = web.AppRunner(app)
    await runner.setup()

    site_http = web.TCPSite(runner, "0.0.0.0", http_port)
    await site_http.start()
    print(f"HTTP  listening on :{http_port}")

    site_https = web.TCPSite(runner, "0.0.0.0", https_port, ssl_context=ssl_context)
    await site_https.start()
    print(f"HTTPS listening on :{https_port}")

    # Block forever
    await asyncio.Event().wait()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="PingCloud web server")
    parser.add_argument("--config", default=None, help="Path to config.yaml")
    parser.add_argument("--web-port", type=int, default=None, help="Web server port")
    parser.add_argument(
        "--http-port", type=int, default=None,
        help="HTTP port when SSL is enabled (default: 80)"
    )
    parser.add_argument(
        "--ssl-cert", default=None, help="Path to SSL certificate PEM file"
    )
    parser.add_argument(
        "--ssl-key", default=None, help="Path to SSL private key file"
    )
    args = parser.parse_args()

    file_cfg = load_config_file(args.config)
    port = args.web_port or file_cfg.get("web_port", DEFAULTS["web_port"])

    ssl_cert = args.ssl_cert or file_cfg.get("ssl_cert")
    ssl_key = args.ssl_key or file_cfg.get("ssl_key")

    if ssl_cert and ssl_key:
        ssl_context = _build_ssl_context(ssl_cert, ssl_key)
        http_port = args.http_port or file_cfg.get("http_port", 80)
        https_port = port
        print(f"SSL enabled: cert={ssl_cert}, key={ssl_key}")
        web.run_app(
            _run_dual(create_app(), http_port, https_port, ssl_context)
        )
    else:
        web.run_app(create_app(), host="0.0.0.0", port=port)
