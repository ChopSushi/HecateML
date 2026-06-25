from google.adk.agents import Agent, SequentialAgent
from google.adk.models.lite_llm import LiteLlm

from .budget import before_model_callback, before_tool_callback
from .prompts import researcher_instruction, verifier_instruction
from .tools import get_fred_series, search_arxiv, search_wikipedia

# Model strategy (LiteLlm reads ANTHROPIC_API_KEY / GEMINI_API_KEY from env):
#
# PRIMARY is Anthropic Claude — reliable, high-quota, strong at tool-calling and
# synthesis, so development and the researcher+verifier pipeline run smoothly.
#
# FALLBACKS are the FREE Google AI Studio Gemini models. This is the key property
# for the assessment: if no paid key is present (e.g. a reviewer reproducing for
# $0), LiteLlm cascades to the free Gemini tier and the agent still works. The
# fallbacks also absorb any transient Anthropic error. Free-tier note: the
# gemini-2.5* models cap at ~20 requests/day; gemini-3.1-flash-lite has more
# headroom, so it leads the free chain.
MODEL = "anthropic/claude-haiku-4-5-20251001"
FALLBACKS = [
    "gemini/gemini-3.1-flash-lite",
    "gemini/gemini-2.5-flash-lite",
    "gemini/gemini-2.5-flash",
]

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
