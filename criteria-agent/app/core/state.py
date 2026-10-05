"""Agent state helpers."""

from strands.agent.state import AgentState


def remember(state: AgentState, key: str, values: list) -> None:
    """Append values to a list in agent state."""
    state.set(key, [*(state.get(key) or []), *values])
