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
