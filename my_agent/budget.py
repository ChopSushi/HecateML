"""Tool-budget enforcement via ADK callbacks.

Two limits, both supplied by the CLI and read from session state:

  - max_tools_per_round: how many tool calls the model may make in one turn
  - max_rounds:          how many model->tools turns are allowed total

Mechanism:
  * `before_model_callback` fires once per model turn -> increment the round
    counter and reset the per-round tool counter.
  * `before_tool_callback` fires before each tool runs -> increment the per-round
    counter; if the call would exceed either budget, return a dict to
    short-circuit the tool (ADK uses the returned dict as the tool response and
    skips the real call), telling the model to stop calling tools and answer.

State keys (seeded by the CLI in `initial_state`):
  budget_max_tools, budget_max_rounds   -> the limits
  budget_round, budget_tools_this_round -> live counters (created on first use)
  budget_rejections                     -> list of rejected calls, for the log
"""

# Sensible defaults if the CLI didn't seed state (e.g. adk web runs).
_DEFAULT_MAX_TOOLS = 2
_DEFAULT_MAX_ROUNDS = 2


def _limits(state):
    return (
        int(state.get("budget_max_tools", _DEFAULT_MAX_TOOLS)),
        int(state.get("budget_max_rounds", _DEFAULT_MAX_ROUNDS)),
    )


def before_model_callback(callback_context, llm_request):
    """Start of a model turn: advance the round, reset the per-round tool count."""
    state = callback_context.state
    state["budget_round"] = int(state.get("budget_round", 0)) + 1
    state["budget_tools_this_round"] = 0
    return None  # don't override the model call


def before_tool_callback(tool, args, tool_context):
    """Enforce per-round and total-round tool budgets.

    Returns a dict (short-circuiting the tool) when over budget, else None.
    """
    state = tool_context.state
    max_tools, max_rounds = _limits(state)
    rnd = int(state.get("budget_round", 1))
    used = int(state.get("budget_tools_this_round", 0))

    # Past the allowed number of rounds: refuse all further tool calls.
    if rnd > max_rounds:
        return _reject(
            state, tool, args, rnd,
            f"Round budget reached (max_rounds={max_rounds}). Do not call more "
            "tools; synthesize your answer from what you already have.",
        )

    # Within an allowed round, but this call exceeds the per-round tool budget.
    if used >= max_tools:
        return _reject(
            state, tool, args, rnd,
            f"Per-round tool budget reached (max_tools={max_tools}). Wait for the "
            "current results; do not call more tools this round.",
        )

    # Allowed: count it and let it run.
    state["budget_tools_this_round"] = used + 1
    return None


def _reject(state, tool, args, rnd, reason):
    """Record a rejected call (for the run log) and return the short-circuit dict."""
    rejections = state.get("budget_rejections", [])
    rejections.append({"tool": getattr(tool, "name", str(tool)),
                       "args": dict(args) if args else {}, "round": rnd,
                       "reason": reason})
    state["budget_rejections"] = rejections
    return {"budget_rejected": True, "reason": reason}
