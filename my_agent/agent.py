from google.adk.agents import Agent
from google.adk.models.lite_llm import LiteLlm

from .budget import before_model_callback, before_tool_callback
from .prompts import researcher_instruction
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

root_agent = Agent(
    name="my_agent",
    model=LiteLlm(model=MODEL, fallbacks=FALLBACKS),
    description=(
        "An autonomous research assistant for a banking startup. Searches "
        "Wikipedia, arXiv, and FRED to answer leadership questions with citations."
    ),
    instruction=researcher_instruction(),
    tools=[search_wikipedia, search_arxiv, get_fred_series],
    before_model_callback=before_model_callback,
    before_tool_callback=before_tool_callback,
)
