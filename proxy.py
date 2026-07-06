"""
proxy.py  —  Public read-only dashboard proxy for loftalgotrades.in
Runs on port 8100 (map this port via Cloudflare Tunnel to loftalgotrades.in)

Proxies GET-only requests to:
  Crypto Bot   →  localhost:8000   (Delta Exchange AlgoBot)
  Auto Bot     →  localhost:8001   (Groww NSE AlgoBot)

Run:
  uvicorn proxy:app --host 0.0.0.0 --port 8100

Or:
  python proxy.py
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware

load_dotenv(Path(__file__).parent / ".env")

CRYPTO_BASE    = os.getenv("CRYPTO_BOT_URL",  "http://localhost:8000")
AUTOBOT_BASE   = os.getenv("AUTOBOT_URL",      "http://localhost:8001")
ALGO_V2_BASE   = os.getenv("ALGO_V2_URL",      "http://localhost:8010")
CRYPTO_SECRET  = os.getenv("CRYPTO_SECRET",   "")
AUTOBOT_SECRET = os.getenv("AUTOBOT_SECRET",  "")
PORT           = int(os.getenv("PUBLIC_DASHBOARD_PORT", "8100"))
CACHE_TTL      = float(os.getenv("CACHE_TTL_SECONDS", "15"))

_CACHE: dict[str, tuple[float, object]] = {}
_HTML_PATH = Path(__file__).parent / "index.html"
_ALGO_V2_DASHBOARD_PATH = Path(
    os.getenv(
        "ALGO_V2_DASHBOARD_PATH",
        str(Path(__file__).resolve().parent.parent / "algo_v2" / "dashboard.html"),
    )
)

app = FastAPI(title="LoftAlgoTrades Public Dashboard", docs_url=None, redoc_url=None)

# Block every HTTP method except GET, HEAD, OPTIONS at the outermost layer.
# FastAPI would return 405 anyway for undefined methods, but this makes it
# explicit and ensures nothing slips through even if new routes are added.
class _ReadOnlyGuard(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            return Response(
                content='{"detail":"Method not allowed — this is a read-only public dashboard"}',
                status_code=405,
                media_type="application/json",
            )
        return await call_next(request)

app.add_middleware(_ReadOnlyGuard)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

# ── Explicit read-only path allowlists ─────────────────────────────────────

_CRYPTO_ALLOWED = frozenset({
    "status",
    "adaptive-status",
    "confidence-calibration",
    "auto-apply-status",
    "tf-thresholds",
    "symbols",
    "signal-analysis/tf/1",
    "signal-analysis/tf/5",
    "signal-analysis/tf/15",
    "signal-analysis/tf/30",
    "signal-analysis/tf/60",
    "signal-analysis/tf/240",
})

_AUTOBOT_ALLOWED = frozenset({
    "status",
    "adaptive-status",
    "confidence-calibration",
    "auto-apply-status",
    "tf-thresholds",
    "symbols",
    "management-analysis",
    "daily-analysis",
    "daily-analysis/tracker",
    "llm-audit/recent",
    "premarket-screener",
    "swing-screener",
    "signal-analysis/tf/5",
    "signal-analysis/tf/15",
    "signal-analysis/tf/30",
    "signal-analysis/tf/60",
    "signal-analysis/tf/240",
})

_ALGO_V2_ALLOWED = frozenset({
    "activity",
    "analytics/daily",
    "analytics/monthly",
    "analytics/sectors",
    "analytics/strategies",
    "analytics/symbols",
    "candidates/top-passed",
    "candidates/top-ranked",
    "config",
    "feed/health",
    "fundamentals/universe",
    "health",
    "intraday/promoted",
    "live/status",
    "nse/events",
    "news/sentiment",
    "pdh_paper/status",
    "positions",
    "premarket/result",
    "readiness",
    "regime",
    "rsi-cycle",
    "session",
    "shortlists",
    "signals",
    "swing/candidates",
    "swing/monitoring",
    "swing/readiness",
    "tracker/history",
    "trades",
    "universe/exploding",
    "universe/ranked",
    "universe/summary",
})


def _is_allowed(path: str, allowlist: frozenset[str]) -> bool:
    clean = path.strip("/").split("?")[0]
    return clean in allowlist


def _algo_v2_dashboard_html() -> str:
    html = _ALGO_V2_DASHBOARD_PATH.read_text(encoding="utf-8")
    html = html.replace("const BASE = 'http://localhost:8010';", "const BASE = '/api/algo-v2';")
    html = html.replace('const BASE = "http://localhost:8010";', "const BASE = '/api/algo-v2';")
    html = html.replace(
        "</style>",
        """
  /* Public proxy is read-only. Hide local write controls while keeping the dashboard usable. */
  .btn-close, .btn-save { display: none !important; }
  .ctrl-input { pointer-events: none; opacity: .65; }
</style>""",
        1,
    )
    return html


async def _proxy(base_url: str, path: str, secret: str, query: str = "") -> object:
    key = f"{base_url}|{path}|{query}"
    now = time.monotonic()
    cached = _CACHE.get(key)
    if cached and (now - cached[0]) < CACHE_TTL:
        return cached[1]

    headers: dict[str, str] = {}
    if secret:
        headers["X-Webhook-Secret"] = secret

    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            url = f"{base_url.rstrip('/')}/{path.lstrip('/')}"
            if query:
                url = f"{url}?{query}"
            r = await client.get(
                url,
                headers=headers,
            )
            r.raise_for_status()
            data = r.json()
    except httpx.ConnectError:
        data = {"offline": True, "error": "Service unreachable"}
    except httpx.TimeoutException:
        data = {"offline": True, "error": "Request timed out"}
    except Exception as exc:
        data = {"offline": True, "error": str(exc)}

    _CACHE[key] = (now, data)
    return data


# ── Routes ─────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def serve_dashboard():
    if not _HTML_PATH.exists():
        return HTMLResponse("<h1>Dashboard not found — index.html missing</h1>", status_code=404)
    return HTMLResponse(_HTML_PATH.read_text(encoding="utf-8"))


@app.get("/api/crypto/{path:path}")
async def crypto_proxy(path: str, request: Request):
    if not _is_allowed(path, _CRYPTO_ALLOWED):
        raise HTTPException(status_code=403, detail="Endpoint not permitted on public dashboard")
    return JSONResponse(await _proxy(CRYPTO_BASE, path, CRYPTO_SECRET, request.url.query))


@app.get("/api/autobot/{path:path}")
async def autobot_proxy(path: str, request: Request):
    if not _is_allowed(path, _AUTOBOT_ALLOWED):
        raise HTTPException(status_code=403, detail="Endpoint not permitted on public dashboard")
    return JSONResponse(await _proxy(AUTOBOT_BASE, path, AUTOBOT_SECRET, request.url.query))


@app.get("/algo-v2", response_class=HTMLResponse)
@app.get("/algo-v2/", response_class=HTMLResponse)
async def serve_algo_v2_dashboard():
    if not _ALGO_V2_DASHBOARD_PATH.exists():
        return HTMLResponse("<h1>algo_v2 dashboard not found</h1>", status_code=404)
    return HTMLResponse(_algo_v2_dashboard_html())


@app.get("/api/algo-v2/{path:path}")
async def algo_v2_proxy(path: str, request: Request):
    if not _is_allowed(path, _ALGO_V2_ALLOWED):
        raise HTTPException(status_code=403, detail="Endpoint not permitted on public dashboard")
    return JSONResponse(await _proxy(ALGO_V2_BASE, path, "", request.url.query))


@app.get("/health")
async def health():
    return {"status": "ok", "service": "loftalgotrades-public-dashboard", "port": PORT}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("proxy:app", host="0.0.0.0", port=PORT, reload=False)
