"""Free, no-paid-key research tools for the agent.

Each function is an ADK tool: a typed Python function whose docstring becomes the
tool description the model sees. All use only `requests` (already installed) and
hit free public APIs.

Sources:
  - Wikipedia REST + Action API  (no key)
  - arXiv Atom API               (no key)
  - FRED (St. Louis Fed) JSON API (free key via FRED_API_KEY; degrades gracefully)

Resilience (CP2): every outbound HTTP call goes through `_request`, which retries
up to 3 times with exponential backoff on network errors and 5xx/429 responses.
After 3 failures the tool returns a structured error dict (never raises), so the
agent can fall back to another source. Every successful result carries a real UTC
`accessed_at` timestamp captured at fetch time, for use in citations.
"""

import os
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import requests

_TIMEOUT = 20
_UA = {"User-Agent": "HecateML-research-agent/0.1 (interview demo)"}

_MAX_ATTEMPTS = 3
# Backoff base 3s -> waits ~3s then ~6s between attempts. arXiv asks for ~3s
# between requests and *extends* its penalty window on repeated 429s, so a short
# wait just re-fails inside the window. 3s/6s gives the limiter real time to clear.
_BACKOFF_BASE = 3.0
_BACKOFF_CAP = 30.0  # never wait more than this between attempts
# Status codes worth retrying (transient): rate-limit + server errors.
_RETRY_STATUS = {429, 500, 502, 503, 504}


def _now_iso() -> str:
    """Current UTC time as an ISO-8601 string, for citation 'accessed_at'."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class ToolRequestError(Exception):
    """Raised internally when all retry attempts are exhausted.

    Carries the attempt count and the *specific* last error detail (e.g.
    "HTTP 429", "ConnectionError: ...") so callers and the run log can record
    exactly why a source failed, not just that it did.
    """

    def __init__(self, message, attempts, detail=None, status=None):
        super().__init__(message)
        self.attempts = attempts
        self.detail = detail or message
        self.status = status  # HTTP status code if applicable, else None


def _request(method, url, **kwargs):
    """HTTP request with 3 attempts + backoff on transient failures.

    Retries on connection/timeout errors and on 429/5xx responses. Honors a
    server-sent `Retry-After` header when present (the correct way to back off a
    429), otherwise uses exponential backoff (capped). Returns
    `(Response, attempt)` on success; raises `ToolRequestError` after the final
    attempt with the specific last-error detail for logging.
    """
    kwargs.setdefault("timeout", _TIMEOUT)
    kwargs.setdefault("headers", _UA)
    last_detail = None
    last_status = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        wait = _BACKOFF_BASE * (2 ** (attempt - 1))  # 3s, 6s, ...
        try:
            resp = requests.request(method, url, **kwargs)
            if resp.status_code in _RETRY_STATUS:
                last_status = resp.status_code
                last_detail = f"HTTP {resp.status_code}"
                # Prefer the server's Retry-After hint if it sent one.
                ra = resp.headers.get("Retry-After")
                if ra:
                    try:
                        wait = min(float(ra), _BACKOFF_CAP)
                    except ValueError:
                        pass
                raise requests.HTTPError(last_detail, response=resp)
            resp.raise_for_status()  # non-retryable 4xx -> raise immediately
            return resp, attempt
        except requests.HTTPError as e:
            resp = getattr(e, "response", None)
            if resp is not None and resp.status_code not in _RETRY_STATUS:
                # Non-retryable client error (e.g. 404): stop now.
                raise ToolRequestError(
                    f"HTTP {resp.status_code} for {url}", attempt,
                    detail=f"HTTP {resp.status_code}", status=resp.status_code,
                )
            last_detail = last_detail or str(e)
        except requests.RequestException as e:
            last_detail = f"{type(e).__name__}: {e}"
        if attempt < _MAX_ATTEMPTS:
            time.sleep(min(wait, _BACKOFF_CAP))
    raise ToolRequestError(
        f"{last_detail} (failed after {_MAX_ATTEMPTS} attempts)",
        _MAX_ATTEMPTS, detail=last_detail, status=last_status,
    )


def search_wikipedia(query: str) -> dict:
    """Search Wikipedia and return a summary of the best-matching article.

    Use this for regulatory history, definitions, institutions, and general
    background (e.g. "Basel III", "Federal Reserve discount window").

    Args:
        query: What to look up, e.g. "Basel III" or "credit risk".

    Returns:
        A dict with the article title, summary extract, source URL, and
        accessed_at timestamp, or an "error" key (with retry count) if the
        lookup failed so the agent can try another source.
    """
    try:
        search, _ = _request(
            "GET",
            "https://en.wikipedia.org/w/api.php",
            params={
                "action": "query",
                "list": "search",
                "srsearch": query,
                "format": "json",
                "srlimit": 1,
            },
        )
        hits = search.json().get("query", {}).get("search", [])
        if not hits:
            return {"error": f"No Wikipedia article found for {query!r}.",
                    "source": "Wikipedia"}
        title = hits[0]["title"]

        rest, _ = _request(
            "GET",
            f"https://en.wikipedia.org/api/rest_v1/page/summary/{title.replace(' ', '_')}",
        )
        data = rest.json()
        return {
            "title": data.get("title", title),
            "summary": data.get("extract", ""),
            "url": data.get("content_urls", {}).get("desktop", {}).get("page")
            or f"https://en.wikipedia.org/wiki/{title.replace(' ', '_')}",
            "source": "Wikipedia",
            "accessed_at": _now_iso(),
        }
    except ToolRequestError as e:
        return {"error": f"Wikipedia lookup failed: {e}", "detail": e.detail,
                "status": e.status, "attempts": e.attempts, "source": "Wikipedia"}
    except Exception as e:  # noqa: BLE001 - tool must not crash the agent
        return {"error": f"Wikipedia lookup failed: {e}", "detail": str(e),
                "source": "Wikipedia"}


def search_arxiv(query: str, max_results: int = 3) -> dict:
    """Search arXiv for recent academic papers and return their abstracts.

    Use this for "recent papers on ..." style questions (e.g. credit risk
    modeling, machine learning for finance).

    Args:
        query: Topic to search, e.g. "credit risk modeling".
        max_results: How many papers to return (default 3, max 10).

    Returns:
        A dict with a list of papers (title, authors, abstract, url, published)
        and an accessed_at timestamp, or an "error" key (with retry count).
    """
    try:
        max_results = max(1, min(int(max_results), 10))
        resp, _ = _request(
            "GET",
            "http://export.arxiv.org/api/query",
            params={
                "search_query": f"all:{query}",
                "start": 0,
                "max_results": max_results,
                "sortBy": "relevance",
            },
        )
        ns = {"a": "http://www.w3.org/2005/Atom"}
        root = ET.fromstring(resp.text)
        papers = []
        for entry in root.findall("a:entry", ns):
            authors = [
                a.findtext("a:name", default="", namespaces=ns)
                for a in entry.findall("a:author", ns)
            ]
            papers.append(
                {
                    "title": (entry.findtext("a:title", "", ns) or "").strip(),
                    "authors": authors[:6],
                    "abstract": (entry.findtext("a:summary", "", ns) or "").strip(),
                    "published": entry.findtext("a:published", "", ns),
                    "url": entry.findtext("a:id", "", ns),
                }
            )
        if not papers:
            return {"error": f"No arXiv papers found for {query!r}.", "source": "arXiv"}
        return {"query": query, "papers": papers, "source": "arXiv",
                "accessed_at": _now_iso()}
    except ToolRequestError as e:
        return {"error": f"arXiv search failed: {e}", "detail": e.detail,
                "status": e.status, "attempts": e.attempts, "source": "arXiv"}
    except Exception as e:  # noqa: BLE001
        return {"error": f"arXiv search failed: {e}", "detail": str(e),
                "source": "arXiv"}


def get_fred_series(series_id: str) -> dict:
    """Look up the latest value of a Federal Reserve economic data series.

    Use this for hard economic numbers: interest rates, GDP, unemployment, etc.
    Common series IDs: FEDFUNDS (fed funds rate), DPCREDIT (discount window
    primary credit rate), UNRATE (unemployment), GDP, CPIAUCSL (CPI).

    Requires a free FRED_API_KEY in the environment; if absent, returns a clear
    message so the agent can fall back to other sources.

    Args:
        series_id: The FRED series ID, e.g. "FEDFUNDS" or "DPCREDIT".

    Returns:
        A dict with the latest observation (date, value), series id, source, and
        accessed_at timestamp, or an "error"/"note" key.
    """
    key = os.environ.get("FRED_API_KEY")
    if not key:
        return {
            "note": "FRED_API_KEY not set; skipping FRED. Use Wikipedia/arXiv "
            "for this question or set a free key from fredaccount.stlouisfed.org.",
            "source": "FRED (St. Louis Fed)",
        }
    try:
        resp, _ = _request(
            "GET",
            "https://api.stlouisfed.org/fred/series/observations",
            params={
                "series_id": series_id,
                "api_key": key,
                "file_type": "json",
                "sort_order": "desc",
                "limit": 1,
            },
        )
        obs = resp.json().get("observations", [])
        if not obs:
            return {"error": f"No FRED data for series {series_id!r}.",
                    "source": "FRED (St. Louis Fed)"}
        latest = obs[0]
        return {
            "series_id": series_id,
            "date": latest.get("date"),
            "value": latest.get("value"),
            "url": f"https://fred.stlouisfed.org/series/{series_id}",
            "source": "FRED (St. Louis Fed)",
            "accessed_at": _now_iso(),
        }
    except ToolRequestError as e:
        return {"error": f"FRED lookup failed: {e}", "detail": e.detail,
                "status": e.status, "attempts": e.attempts,
                "source": "FRED (St. Louis Fed)"}
    except Exception as e:  # noqa: BLE001
        return {"error": f"FRED lookup failed: {e}", "detail": str(e),
                "source": "FRED (St. Louis Fed)"}
