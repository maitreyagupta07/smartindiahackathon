"""
Unit tests for router/classifier + model_registry.
"""
from router.classifier import classify_task
from router.model_registry import get_model_for_task_type, TEXT_MODEL, VISION_MODEL


def test_classify_vision():
    assert classify_task("what is in this photo", "image/png") == "vision"


def test_classify_code_execution():
    assert classify_task("please calculate the flow rate", None) == "code-execution"


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
