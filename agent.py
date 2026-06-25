#!/usr/bin/env python3
"""HecateML research agent — command-line entrypoint.

Usage:
    python agent.py "What is Basel III?"
    python agent.py "What is Basel III?" --max-tools 1 --max-rounds 3
    python agent.py "What is Basel III?" 1 3          # positional convenience

Prints ONLY the final answer to stdout; progress/diagnostics go to stderr.

`--max-tools` / `--max-rounds` are parsed here (defaults 2 / 2) and threaded into
the run config. Enforcement of those budgets lands in CP3; for now they are
captured and echoed to stderr so the wiring is in place.
"""

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Load .env so the CLI works standalone (adk web loads it on its own).
_ENV = Path(__file__).resolve().parent / ".env"
if _ENV.exists():
    for line in _ENV.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

from google.adk.runners import InMemoryRunner  # noqa: E402  (after .env load)
from google.genai import types  # noqa: E402

from my_agent.agent import root_agent  # noqa: E402


def log(*a):
    """Diagnostics to stderr so stdout stays clean (just the answer)."""
    print(*a, file=sys.stderr, flush=True)


def parse_args(argv):
    """Parse question + budgets.

    Supports flags (--max-tools/--max-rounds) and a positional convenience form
    `"<question>" <max_tools> <max_rounds>`.
    """
    p = argparse.ArgumentParser(
        prog="agent.py",
        description="HecateML banking research agent (free models + sources).",
        epilog='Positional convenience: agent.py "<question>" <max_tools> <max_rounds>',
    )
    p.add_argument("question", help="The research question, quoted.")
    p.add_argument("pos_tools", nargs="?", type=int, default=None,
                   help="(optional) max tools/round, positional form.")
    p.add_argument("pos_rounds", nargs="?", type=int, default=None,
                   help="(optional) max rounds, positional form.")
    p.add_argument("--max-tools", type=int, default=None,
                   help="Max independent tools per round (default 2).")
    p.add_argument("--max-rounds", type=int, default=None,
                   help="Max rounds of tool-calling (default 2).")
    args = p.parse_args(argv)

    # Flags win over positionals; both fall back to defaults (2 / 2).
    max_tools = args.max_tools if args.max_tools is not None else args.pos_tools
    max_rounds = args.max_rounds if args.max_rounds is not None else args.pos_rounds
    args.max_tools = 2 if max_tools is None else max_tools
    args.max_rounds = 2 if max_rounds is None else max_rounds
    if args.max_tools < 1 or args.max_rounds < 1:
        p.error("max-tools and max-rounds must be >= 1")
    return args


async def run(question, max_tools, max_rounds):
    runner = InMemoryRunner(agent=root_agent, app_name="hecate")
    # Seed the tool budget into session state; the budget callbacks read it.
    session = await runner.session_service.create_session(
        app_name="hecate",
        user_id="cli",
        state={"budget_max_tools": max_tools, "budget_max_rounds": max_rounds},
    )
    msg = types.Content(role="user", parts=[types.Part(text=question)])

    final_text = ""
    raw_events = []  # full ADK event stream — the complete ground-truth trace
    async for event in runner.run_async(
        user_id="cli", session_id=session.id, new_message=msg
    ):
        raw_events.append(event)
        who = event.author or "?"
        for part in (event.content.parts if event.content else []):
            if getattr(part, "function_call", None):
                log(f"[{who}] tool {part.function_call.name}({dict(part.function_call.args)})")
            if getattr(part, "function_response", None):
                resp = part.function_response.response
                if isinstance(resp, dict) and resp.get("budget_rejected"):
                    log(f"[{who}] budget REJECTED {part.function_response.name}: {resp['reason']}")
            if getattr(part, "text", None):
                final_text = part.text  # latest text = verifier's final answer (runs last)

    final_state = (await runner.session_service.get_session(
        app_name="hecate", user_id="cli", session_id=session.id)).state
    # Budget rounds are tracked per-agent (budget_round::<agent>); sum them.
    rounds = {k.split("::", 1)[1]: v for k, v in final_state.items()
              if k.startswith("budget_round::")}
    log(f"[budget] rounds_by_agent={rounds} "
        f"rejections={len(final_state.get('budget_rejections', []))}")

    _write_run_log(question, max_tools, max_rounds, raw_events)
    return final_text.strip()


def _write_run_log(question, max_tools, max_rounds, raw_events):
    """Persist the full raw ADK event stream for this run to runs/run-<ts>.json.

    ADK already emits every model turn, tool call, tool response, and final text
    as an Event; we dump each verbatim via model_dump(mode="json") so the log is
    the complete, lossless trace rather than a hand-picked subset.
    """
    runs_dir = Path(__file__).resolve().parent / "runs"
    runs_dir.mkdir(exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = runs_dir / f"run-{ts}.json"
    record = {
        "timestamp": ts,
        "question": question,
        "budget": {"max_tools": max_tools, "max_rounds": max_rounds},
        "events": [e.model_dump(mode="json", exclude_none=True) for e in raw_events],
    }
    path.write_text(json.dumps(record, indent=2))
    log(f"[log] wrote {path}")


def main():
    args = parse_args(sys.argv[1:])
    log(f"[config] max_tools={args.max_tools} max_rounds={args.max_rounds}")
    log(f"[question] {args.question}")
    answer = asyncio.run(run(args.question, args.max_tools, args.max_rounds))
    if not answer:
        log("[error] no answer produced")
        sys.exit(1)
    print(answer)  # the ONLY thing on stdout


if __name__ == "__main__":
    main()
