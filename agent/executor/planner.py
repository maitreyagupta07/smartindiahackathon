"""
Very small planning stub: decides the next action given current state.
Replace with real multi-step planning logic.
"""
from executor.state import TaskState


def plan_next_action(state: TaskState) -> str:
    """
    Returns one of: "call_model", "call_tool", "finalize"
    Stubbed as a single-step loop for now — extend for real multi-step tasks.
    """
    if not state.steps:
        return "call_model"
    return "finalize"
