# HecateML — Multi-Tool Research Agent

An autonomous research agent for a banking/finance setting. It takes a natural
language question, plans which sources to search, executes the searches, and
returns a **synthesized answer with verifiable citations** — strictly separating
**facts** (retrieved from a source) from **claims** (the model's own inference).

It runs on **free, reproducible infrastructure**: a free Google AI Studio (Gemini)
model and free, mostly key-less data APIs (Wikipedia, arXiv, FRED). A reviewer can
clone, install two packages, add one free key, and run it — **no paid API key
required**.

```bash
.venv/bin/python agent.py "What is the Federal Reserve's discount window and how does it work?"
```

---

## Quickstart

**Requirements:** Python **3.11**.

```bash
# 1. clone, then create a venv and install the two dependencies
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 2. add a free model key (one line) to a .env file in the repo root
echo "GEMINI_API_KEY=your_free_key" > .env        # from https://aistudio.google.com/apikey
# optional, for the data-retrieval question (instant free signup):
echo "FRED_API_KEY=your_free_key" >> .env          # from https://fredaccount.stlouisfed.org/apikeys

# 3. ask a question
.venv/bin/python agent.py "What are the Basel III capital requirements for banks?"
```

The final answer prints to **stdout**; all diagnostics (tool calls, budget,
log path) go to **stderr**, so `... 2>/dev/null` yields a clean, pipeable answer.

**Budget flags** (optional) control tool usage:

```bash
.venv/bin/python agent.py "<question>" --max-tools 1 --max-rounds 3
.venv/bin/python agent.py "<question>" 1 3          # positional shorthand
```

> **Why `.venv/bin/python` and not just `python`?** Some shells alias `python`
> to a system interpreter that lacks the dependencies. Calling the venv's
> interpreter directly is alias-proof and guarantees the right environment.

---

## Architecture

```
            question (CLI)
                 │
                 ▼
        ┌─────────────────┐   output_key="draft"   ┌─────────────────┐
        │   RESEARCHER    │ ─────────────────────▶ │    VERIFIER     │
        │  plans + calls  │                         │ re-checks facts │
        │  tools, drafts  │                         │ + final format  │
        └─────────────────┘                         └─────────────────┘
                 │     ▲                                   │     ▲
                 ▼     │ tool budget (callbacks)           ▼     │
        ┌───────────────────────────────────────────────────────────┐
        │   TOOLS:  search_wikipedia · search_arxiv · get_fred_series │
        │   (retry 3× + backoff, accessed_at stamps, structured errs) │
        └───────────────────────────────────────────────────────────┘
                 │
                 ▼
        final answer (stdout)  +  raw event trace (runs/run-<ts>.json)
```

### Orchestration pattern: **plan-and-execute, two-stage verify (researcher → verifier)**

Built on **Google ADK** (`SequentialAgent`), driven by an LLM via **LiteLLM**:

1. **Researcher** — classifies the question (single-source, academic, FRED data,
   multi-source/cross-tool synthesis, out-of-scope, speculative), writes a short
   plan naming the *independent* tools it will call, executes them, and drafts a
   fact-based answer. Its draft is saved to session state (`output_key="draft"`).
2. **Verifier** — reads the draft, **ranks facts by importance**, and re-checks the
   most load-bearing ones. It prefers **"unbiasing"** (re-derive a fact from a
   different query angle) and falls back to **source-checking** (confirm the cited
   source actually contains the claim). It then emits the final answer, tagging
   each fact `[VERIFIED]` / `[SOURCE-CHECKED]` / `[UNVERIFIED]` / `[CONTRADICTED]`.

**Why this pattern over plain ReAct?** A single ReAct loop tends to assert
plausible-but-unchecked facts. Splitting *generation* (researcher) from
*adversarial verification* (verifier) gives a second, independent pass whose only
job is to challenge the draft — which is exactly where citation errors and
overconfident claims get caught. The verifier **annotates rather than deletes**, so
nothing is silently dropped; the reader sees what was confirmed and what wasn't.

### Fact vs. claim (a core design goal)

- A **fact** is directly supported by a tool result and carries a `[n]` citation.
- A **claim** is the model's inference/synthesis connecting facts — labeled as
  interpretation and **not** cited to a source (no source said it).

This distinction is enforced in the prompts and is visible in every answer.

### Tools

Each tool ([my_agent/tools.py](my_agent/tools.py)) is a self-contained function with
a consistent contract: a typed signature, a docstring the LLM uses as the tool
description, and a `dict` return that always carries a `source` and (on success) an
`accessed_at` UTC timestamp, or a structured `error`/`note` on failure.

| Tool | API | Key? | Use |
|------|-----|------|-----|
| `search_wikipedia` | Wikipedia REST + Action | none | definitions, regulation, institutions |
| `search_arxiv` | arXiv Atom | none | recent academic papers |
| `get_fred_series` | FRED JSON | free key | US economic data (UNRATE, FEDFUNDS, GDP…) |

Adding a tool is a plug-in operation: write the typed function, add it to the
`_TOOLS` list. No other wiring changes.

### Model strategy (free by default)

Configured in [my_agent/agent.py](my_agent/agent.py) via LiteLLM, which makes the
model a one-line swap:

- **Primary:** `gemini/gemini-3.1-flash-lite` (free Google AI Studio tier).
- **Fallbacks:** other free Gemini tiers, cascading on rate-limit/error. Anthropic
  Haiku is appended **only if `ANTHROPIC_API_KEY` is set** — a developer's optional
  last resort. Without that key the chain stays 100% free, so a reviewer never hits
  an auth error from a keyless paid fallback.

**Tradeoffs:** the free Gemini tier is the constraint. `gemini-2.5*` models cap at
~20 requests/day; the `-flash-lite` models have more headroom and lead the chain.
These are small models — capable for retrieval + synthesis, but the
researcher→verifier design (two model calls/question) is deliberately robust to
their occasional misfires, and the fallback chain absorbs rate limits.

---

## Multi-step reasoning (Tier 2)

The agent runs **multiple rounds** of tool calls and maintains state across them
(what's been retrieved, the tool budget, the researcher's draft). Two places where
multi-step beats a single pass:

- **Q1 — Fed discount window.** The researcher retrieved the definition from one
  Wikipedia article. The **verifier independently re-queried** (`"lender of last
  resort Federal Reserve discount window"`) and confirmed the framing from a second
  angle — promoting a fact from `[SOURCE-CHECKED]` to `[VERIFIED]`. A single pass
  would have stopped at one unconfirmed source.
- **Q6 — yield-curve inversions + papers.** A single Wikipedia lookup defines the
  yield curve but can't answer the "recent papers?" half. The agent fans out to
  **Wikipedia *and* arXiv in one plan**, then synthesizes across them (the nuance
  that ML models often *don't* beat the traditional 10y/3m spread comes only from
  the arXiv papers, cited `[2][3]`).

Full outputs for all 8 reference questions are in
[`reference_outputs/`](reference_outputs/).

---

## Observability (Tier 2)

Every run writes a structured trace to `runs/run-<timestamp>.json` containing the
**full raw ADK event stream** — every model turn, tool call, tool input/output,
token usage, and the final synthesis — plus the question and budget. Because it's
the framework's own event objects (`model_dump`), it's lossless: a reviewer can
reconstruct exactly *why* the agent made each decision.

```bash
.venv/bin/python agent.py "What is Basel III?"
python3 -m json.tool runs/run-*.json | less
```

---

## Reliability & resilience

- **Tool retries:** every HTTP call retries 3× with exponential backoff, honoring a
  server `Retry-After` header when present; transient statuses (429/5xx) and
  timeouts retry, 4xx fails fast. After 3 failures the tool returns a *structured
  error* (with the specific reason) rather than crashing.
- **Cross-source fallback:** the agent is instructed that if a source fails it should
  fall back to another and **say so** in the answer. Observed live — when arXiv timed
  out, the run degraded gracefully and reported it instead of fabricating.
- **Model fallback:** the LiteLLM fallback chain absorbs rate limits across models.
- **Tool budget:** `--max-tools` / `--max-rounds` cap tool usage, enforced via ADK
  callbacks; over-budget calls are rejected with a message and logged, and the agent
  answers from what it has.

---

## Performance summary & honest limitations

**What works well:** all 8 reference questions produce cited answers with the
correct tool(s) per type; out-of-scope (#7) is refused without fabrication;
speculative (#8) cleanly separates facts from labeled speculation; the fact/claim
split and verification tags are consistent.

**Known limitations:**

- **FRED is point-in-time.** `get_fred_series` returns only the latest observation,
  so "how has unemployment changed over the past year?" (#5) is answered honestly as
  *unverifiable from the data retrieved* rather than fabricated. A date-range fetch
  would fully answer it.
- **Free-tier quota.** The free Gemini tier's daily caps can throttle heavy runs;
  the fallback chain mitigates but doesn't eliminate this.
- **arXiv flakiness.** arXiv rate-limits/times out under rapid use; retries + backoff
  handle it, but a burst of academic questions can be slow.
- **Small-model variance.** On the free models, synthesis quality varies more than on
  a frontier model; the verifier stage is partly there to compensate.

**What I'd do with more time:**

- A real **evaluation harness** (15+ questions with expected/forbidden facts,
  measuring relevance, source-attribution accuracy, and correct refusal).
- **Synthetic failure injection** in tests to quantify graceful degradation.
- A **tool-routing benchmark** measuring tool-selection accuracy by question type.
- Date-range FRED support and a Semantic Scholar tool for broader academic coverage.

---

## Project layout

```
agent.py              # CLI entrypoint: arg parsing, .env load, run loop, run-log
my_agent/
  agent.py            # researcher + verifier + SequentialAgent; model strategy
  prompts.py          # researcher/verifier instructions + 8 question-type few-shots
  tools.py            # Wikipedia / arXiv / FRED tools; retry, backoff, timestamps
  budget.py           # per-agent tool-budget enforcement via ADK callbacks
requirements.txt      # google-adk, litellm (requests comes transitively)
reference_outputs/    # captured answers + traces for the 8 reference questions
runs/                 # structured JSON traces (gitignored)
```
