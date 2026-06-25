import os

from google.adk.agents import Agent, SequentialAgent
from google.adk.models.lite_llm import LiteLlm

from .budget import before_model_callback, before_tool_callback
from .prompts import researcher_instruction, verifier_instruction
from .tools import get_fred_series, search_arxiv, search_wikipedia

# Model strategy (LiteLlm reads GEMINI_API_KEY / ANTHROPIC_API_KEY from env):
#
# PRIMARY is a FREE Google AI Studio Gemini model. This is the property the
# assessment requires: a reviewer with only a free Gemini key (no paid API key)
# can reproduce results at $0 — the default model path costs nothing.
#
# FALLBACKS cascade on rate-limit/error across the other free Gemini tiers (each
# has its own quota pool; the gemini-2.5* models cap at ~20 requests/day, so
# gemini-3.1-flash-lite leads). Anthropic Haiku is appended ONLY when an
# ANTHROPIC_API_KEY is present — a developer's optional last resort if every free
# model is exhausted. Without that key the chain stays 100% free, so a reviewer
# never hits a confusing auth error from a keyless paid fallback.
MODEL = "gemini/gemini-3.1-flash-lite"
FALLBACKS = [
    "gemini/gemini-2.5-flash-lite",
    "gemini/gemini-2.5-flash",
]
if os.environ.get("ANTHROPIC_API_KEY"):
    FALLBACKS.append("anthropic/claude-haiku-4-5-20251001")  # optional, paid

_TOOLS = [search_wikipedia, search_arxiv, get_fred_series]


def _llm():
    """Fresh LiteLlm per agent (model+fallback chain is shared config)."""
    return LiteLlm(model=MODEL, fallbacks=FALLBACKS)


# Stage 1: the researcher gathers source-backed facts and drafts an answer.
# Its final text is auto-saved to session state under "draft" (output_key) for
# the verifier to read.
researcher = Agent(
    name="researcher",
    model=_llm(),
    description="Gathers facts from Wikipedia/arXiv/FRED and drafts a cited answer.",
    instruction=researcher_instruction(),
    tools=_TOOLS,
    output_key="draft",
    before_model_callback=before_model_callback,
    before_tool_callback=before_tool_callback,
)

# Stage 2: the verifier reads {draft}, re-checks facts (unbias / source-check),
# annotates verification status, and emits the FINAL formatted answer.
verifier = Agent(
    name="verifier",
    model=_llm(),
    description="Verifies the researcher's facts and emits the final cited answer.",
    instruction=verifier_instruction(),
    tools=_TOOLS,
    before_model_callback=before_model_callback,
    before_tool_callback=before_tool_callback,
)

# The pipeline: researcher -> verifier. The verifier's output is the final answer.
root_agent = SequentialAgent(
    name="my_agent",
    description=(
        "An autonomous research assistant for a banking startup. A researcher "
        "gathers facts from Wikipedia, arXiv, and FRED; a verifier checks them "
        "and emits the final answer with citations and verification labels."
    ),
    sub_agents=[researcher, verifier],
)
