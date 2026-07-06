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
import secrets
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
# Secret gating the algo_v2 "Live" tab (real broker funds / holdings / live book).
# Empty → the Live tab is locked for everyone (fail-closed).
ALGO_V2_LIVE_KEY = os.getenv("ALGO_V2_LIVE_KEY", "")
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


# Endpoints that expose the real live portfolio. `live/status` is always
# protected; positions/trades/analytics only expose the live book when queried
# with execution=live (or execution=all), so gate those on the query string.
_ALGO_V2_LIVE_PATHS = frozenset({"live/status"})


def _algo_v2_needs_key(path: str, query: str) -> bool:
    clean = path.strip("/").split("?")[0]
    if clean in _ALGO_V2_LIVE_PATHS:
        return True
    exec_val = ""
    for part in query.split("&"):
        if part.startswith("execution="):
            exec_val = part[len("execution="):].lower()
    return exec_val in ("live", "all")


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
    # Gate the Live tab (real portfolio) behind the dashboard key. Injected only
    # into the public proxy's copy — the local dashboard is never modified.
    html = html.replace("</body>", _ALGO_V2_LIVE_GATE_JS + "\n</body>", 1)
    return html


# Wraps fetch inside the proxied algo_v2 dashboard: any request that would pull
# the real live portfolio (live/status, or execution=live/all) is held until the
# user enters the dashboard key, which is validated server-side, cached in
# sessionStorage, and sent as X-Dashboard-Key on every protected request.
_ALGO_V2_LIVE_GATE_JS = """
<script>
(function(){
  'use strict';
  var STORE = 'lat_live_key';
  var _origFetch = window.fetch.bind(window);
  var _keyPromise = null;

  function urlOf(input){
    try { return typeof input === 'string' ? input : (input && input.url) || ''; }
    catch(e){ return ''; }
  }
  function isProtected(url){
    if (!url) return false;
    return /\\/live\\/status(\\?|$)/.test(url) || /[?&]execution=(live|all)(&|$)/.test(url);
  }
  function validateKey(key){
    return _origFetch('/api/algo-v2/live/status', {headers:{'X-Dashboard-Key':key}})
      .then(function(r){ return r.status === 200; })
      .catch(function(){ return false; });
  }
  function acquireKey(){
    var existing = null;
    try { existing = sessionStorage.getItem(STORE); } catch(e){}
    if (existing) return Promise.resolve(existing);
    if (_keyPromise) return _keyPromise;
    _keyPromise = (async function(){
      while (true){
        var entered = window.prompt('\\uD83D\\uDD12 The Live tab shows real portfolio data (funds & holdings).\\nEnter the dashboard key to unlock:');
        if (entered === null){ _keyPromise = null; return null; }
        var key = entered.trim();
        if (!key) continue;
        if (await validateKey(key)){
          try { sessionStorage.setItem(STORE, key); } catch(e){}
          _keyPromise = null;
          return key;
        }
        window.alert('Incorrect key \\u2014 try again.');
      }
    })();
    return _keyPromise;
  }

  window.fetch = function(input, init){
    var url = urlOf(input);
    if (!isProtected(url)) return _origFetch(input, init);
    return acquireKey().then(function(key){
      if (!key){
        return new Response(JSON.stringify({error:'locked', locked:true}),
          {status:401, headers:{'Content-Type':'application/json'}});
      }
      var opts = Object.assign({}, init);
      var h = new Headers((init && init.headers) ||
        (typeof input === 'object' && input ? input.headers : undefined));
      h.set('X-Dashboard-Key', key);
      opts.headers = h;
      return _origFetch(url, opts).then(function(r){
        // A stored key that later stops working (rotated) — drop it so the
        // next Live request re-prompts instead of silently failing.
        if (r && r.status === 401){ try { sessionStorage.removeItem(STORE); } catch(e){} }
        return r;
      });
    });
  };
})();
</script>"""


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
    if _algo_v2_needs_key(path, request.url.query):
        provided = request.headers.get("X-Dashboard-Key", "")
        if not ALGO_V2_LIVE_KEY or not secrets.compare_digest(provided, ALGO_V2_LIVE_KEY):
            raise HTTPException(status_code=401, detail="Live tab requires the dashboard key")
    return JSONResponse(await _proxy(ALGO_V2_BASE, path, "", request.url.query))


@app.get("/health")
async def health():
    return {"status": "ok", "service": "loftalgotrades-public-dashboard", "port": PORT}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("proxy:app", host="0.0.0.0", port=PORT, reload=False)
