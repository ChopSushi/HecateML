#!/usr/bin/env python3
"""Tier 3 — Tool Reliability & Fallback: synthetic failure injection demo.

Forces tools to fail in controlled ways and shows the agent degrading gracefully
instead of crashing. Run from the repo root:

    .venv/bin/python scripts/demo_resilience.py

Three scenarios, cheapest first (only #3 makes a model call):

  1. RETRY + BACKOFF    — a forced 503 with Retry-After is retried 3x, then
                          surfaces a structured error (no crash). Tool layer only.
  2. STRUCTURED ERROR   — a totally unreachable endpoint returns a clean error
                          dict carrying the specific reason. Tool layer only.
  3. CROSS-SOURCE FALLBACK (end-to-end) — arXiv is force-failed; the agent answers
                          a cross-tool question from Wikipedia and REPORTS the gap.
"""

import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

# Load .env (same loader as the CLI) so scenario 3 has a model key.
_ENV = Path(__file__).resolve().parent.parent / ".env"
if _ENV.exists():
    for line in _ENV.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from my_agent import tools  # noqa: E402


def hr(title):
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")


# --------------------------------------------------------------------------
# Scenario 1: forced 503 + Retry-After -> 3 retries with backoff -> structured error
# --------------------------------------------------------------------------
def scenario_retry_backoff():
    hr("1. RETRY + BACKOFF on a transient 503 (synthetic)")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(503)
            self.send_header("Retry-After", "1")
            self.end_headers()
            self.wfile.write(b"synthetic outage")

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 8799), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    t0 = time.time()
    try:
        tools._request("GET", "http://127.0.0.1:8799/x")
    except tools.ToolRequestError as e:
        dt = time.time() - t0
        print(f"  injected: every request -> HTTP 503 (Retry-After: 1s)")
        print(f"  result:   gave up after {e.attempts} attempts in {dt:.1f}s "
              f"(honored Retry-After, did not crash)")
        print(f"  detail:   status={e.status}, reason={e.detail!r}")
    finally:
        srv.shutdown()


# --------------------------------------------------------------------------
# Scenario 2: unreachable endpoint -> tool returns a clean structured error dict
# --------------------------------------------------------------------------
def scenario_structured_error():
    hr("2. STRUCTURED ERROR from an unreachable source (synthetic)")

    real = tools._request

    def boom(*a, **k):
        raise tools.ToolRequestError(
            "ConnectionError: simulated network failure (failed after 3 attempts)",
            attempts=3, detail="ConnectionError: simulated network failure", status=None)

    tools._request = boom
    try:
        result = tools.search_wikipedia("Basel III")
    finally:
        tools._request = real

    print("  injected: search_wikipedia's HTTP layer always fails")
    print(f"  result:   tool returned a dict (no exception leaked to the agent):")
    print(f"            error  = {result.get('error')!r}")
    print(f"            detail = {result.get('detail')!r}")
    print(f"            source = {result.get('source')!r}")
    print("  -> the agent can read this and fall back to another source.")


# --------------------------------------------------------------------------
# Scenario 3: end-to-end — arXiv force-failed, agent falls back to Wikipedia
# --------------------------------------------------------------------------
def scenario_cross_source_fallback():
    hr("3. CROSS-SOURCE FALLBACK, end-to-end (arXiv force-failed)")
    if not (os.environ.get("GEMINI_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")):
        print("  (skipped: no GEMINI_API_KEY/ANTHROPIC_API_KEY in .env)")
        return

    import asyncio
    from google.adk.runners import InMemoryRunner
    from google.genai import types
    from my_agent.agent import root_agent

    # Force every arXiv call to fail with a structured error. We monkeypatch the
    # arXiv tool's underlying HTTP helper so the tool still exists under its real
    # name (ADK registers tools by __name__) but its network call always fails --
    # exactly what a real arXiv outage looks like to the agent.
    import my_agent.tools as tmod

    real_request = tmod._request

    def failing_request(method, url, **kwargs):
        if "arxiv.org" in url:
            raise tmod.ToolRequestError(
                "synthetic arXiv outage (failed after 3 attempts)",
                attempts=3, detail="synthetic arXiv outage", status=503)
        return real_request(method, url, **kwargs)

    tmod._request = failing_request

    q = ("Explain the relationship between yield curve inversions and recessions. "
         "Are there recent academic papers on this topic?")
    print(f"  injected: search_arxiv -> always fails")
    print(f"  question: {q}\n")

    async def go():
        runner = InMemoryRunner(agent=root_agent, app_name="demo")
        s = await runner.session_service.create_session(
            app_name="demo", user_id="u",
            state={"budget_max_tools": 2, "budget_max_rounds": 2})
        msg = types.Content(role="user", parts=[types.Part(text=q)])
        final = ""
        async for ev in runner.run_async(user_id="u", session_id=s.id, new_message=msg):
            for p in (ev.content.parts if ev.content else []):
                if getattr(p, "function_call", None):
                    print(f"    [{ev.author}] tool {p.function_call.name}")
                if getattr(p, "text", None):
                    final = p.text
        return final

    answer = asyncio.run(go())
    tmod._request = real_request
    print("\n  --- final answer (degraded, should rely on Wikipedia + report the gap) ---")
    print("\n".join("  " + ln for ln in answer.splitlines()))


if __name__ == "__main__":
    scenario_retry_backoff()
    scenario_structured_error()
    scenario_cross_source_fallback()
    print("\nDone. Resilience mechanisms exercised under injected failure.")
