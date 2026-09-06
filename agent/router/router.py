"""
Top-level routing decision: given a task, pick which model to use.
Per §2.5 locked-in model names:
  - text/code: qwen2.5:1.5b-instruct
  - vision:    moondream
"""
from router.classifier import classify, needs_reasoning
from router.model_registry import get_model_for_task_type, TEXT_MODEL


async def route_task(
    prompt: str, file_mime_type: str | None, chat_id: str | None = None
) -> tuple[str, str, bool]:
    """
    Returns (task_type, model_name, needs_reasoning_flag) — unchanged shape,
    still exactly what executor/loop.py expects.

    model_name here is the model for the FIRST step only. For
    task_type == "vision" with needs_reasoning_flag == True, the planner
    (executor/planner.py) is responsible for chaining a second Qwen step
    after Moondream's observation — routing only decides the entry point.

    When `chat_id` is set the request is part of a chat conversation — but
    that alone must NOT force every message down the KB-only chat path.
    A message inside a chat can still be a real action request ("generate
    a word doc of the oil leak in B23", "run this code") rather than a
    knowledge question; forcing chat_id -> task_type="chat" unconditionally
    made every such request search the (often-empty/irrelevant) chat
    Knowledge Base and report "not in Knowledge Base" instead of actually
    generating the file/running the code. So the classifier always runs
    first: only when it finds no strong actionable signal (i.e. it would
    have fallen back to plain "text-generation" anyway) does chat_id
    upgrade that into "chat" — a plain conversational question benefits
    from KB retrieval + conversation history; an explicit document/code/
    search request does not need to detour through the KB at all.
    """
    result = classify(prompt, file_mime_type)

    if chat_id and chat_id.strip() and result.task_type == "text-generation":
        print(
            f"[ROUTER] chat_id={chat_id!r} -> task_type=chat first_model={TEXT_MODEL} "
            f"(no actionable signal in message — treated as a KB/conversation question)"
        )
        return "chat", TEXT_MODEL, False

    model_name = get_model_for_task_type(result.task_type)
    reasoning_flag = needs_reasoning(prompt, file_mime_type)

    # Explainability (item 9): log which signals actually won, not just the
    # final task_type — matched keyword/phrase signals only, never
    # model chain-of-thought (there isn't any at this stage; classification
    # never calls Qwen/Ollama). Log only, never part of the §2.4 response.
    winning_signals = [f"{p!r}(+{w},{g})" for p, w, g, *_ in result.matched_signals.get(result.task_type, [])]
    print(
        f"[ROUTER] task_type={result.task_type} first_model={model_name} "
        f"needs_reasoning={reasoning_flag} confidence={result.confidence} "
        f"multi_step={result.is_multi_step} workflow={result.workflow} "
        f"matched={winning_signals} all_scores={result.scores}"
    )

    return result.task_type, model_name, reasoning_flag
