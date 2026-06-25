from google.adk.agents import Agent
from google.adk.models.lite_llm import LiteLlm

CLAUDE = "anthropic/claude-sonnet-4-6"  # LiteLlm reads ANTHROPIC_API_KEY from env


def get_word_length(word: str) -> dict:
    """Return the number of letters in a word.

    Args:
        word: The word to measure.
    """
    return {"word": word, "length": len(word)}


root_agent = Agent(
    name="my_agent",
    model=LiteLlm(model=CLAUDE),
    description="A Claude-backed assistant with a simple example tool.",
    instruction=(
        "You are a helpful assistant. When the user asks how long a word is, "
        "use the get_word_length tool rather than guessing."
    ),
    tools=[get_word_length],
)
