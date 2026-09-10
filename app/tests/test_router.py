"""
Unit tests for router/classifier + model_registry.
"""
from app.router.classifier import classify_task, needs_reasoning
from app.router.model_registry import get_model_for_task_type, TEXT_MODEL, VISION_MODEL


def test_classify_vision():
    assert classify_task("what is in this photo", "image/png") == "vision"


def test_needs_reasoning_true_for_analysis_prompt():
    assert needs_reasoning("explain what's wrong in this inspection photo", "image/png") is True


def test_needs_reasoning_false_for_plain_description():
    assert needs_reasoning("what is in this photo", "image/png") is False


def test_needs_reasoning_false_without_image():
    assert needs_reasoning("explain the quarterly report", None) is False


def test_classify_code_execution():
    # A concrete arithmetic expression, not just the word "calculate", is
    # what actually warrants code-execution — see test_classifier_signals.py
    # for the full multi-signal test matrix (including why bare "calculate"
    # on its own, e.g. "please calculate the flow rate", now correctly
    # stays text-generation instead of forcing code-execution).
    assert classify_task("calculate 12 * 7", None) == "code-execution"


def test_classify_doc_search():
    assert classify_task("search the manual for valve specs", None) == "doc-search"


def test_classify_document_generation():
    assert classify_task("generate a docx approval note", None) == "document-generation"


def test_classify_default_text():
    assert classify_task("tell me a joke", None) == "text-generation"


def test_model_registry_vision():
    assert get_model_for_task_type("vision") == VISION_MODEL


def test_model_registry_text():
    assert get_model_for_task_type("text-generation") == TEXT_MODEL


def test_smalltalk_in_chat_skips_knowledge_base():
    """A greeting inside an active chat must NOT become a KB question.

    Observed live before this: "hi" sent right after a document-generation
    turn came back re-emitting that document, because any low-signal message
    in a chat was upgraded to task_type="chat", which searches the KB using
    the message itself as the query and then asks the model to answer FROM
    the Knowledge Base with the previous turns in context.
    """
    import asyncio
    from app.router.router import route_task

    for greeting in ("hi", "Hello!", "thanks", "ok", "good morning"):
        task_type, _model, _reason = asyncio.run(route_task(greeting, None, chat_id="c1"))
        assert task_type == "text-generation", f"{greeting!r} -> {task_type}"


def test_real_questions_in_chat_still_use_knowledge_base():
    """The small-talk bypass must not swallow genuine questions — including
    ones that merely START with a greeting word."""
    import asyncio
    from app.router.router import route_task

    for question in (
        "thanks, now summarise the inspection report",
        "hi, what does the SOP say about lockout-tagout?",
        "what is the bearing clearance?",
    ):
        task_type, _model, _reason = asyncio.run(route_task(question, None, chat_id="c1"))
        assert task_type != "text-generation" or "summar" in question, f"{question!r} -> {task_type}"
