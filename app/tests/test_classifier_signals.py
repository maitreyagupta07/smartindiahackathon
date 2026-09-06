"""
Extensive tests for the multi-signal weighted classifier (router/classifier.py).

These test classify()/classify_task() directly — no model/network calls
anywhere, since classification is pure keyword/phrase/pattern scoring (see
the module docstring in classifier.py for why no Ollama/Qwen call is ever
used just to classify a task).
"""
from app.router.classifier import classify, classify_task


# ---------------------------------------------------------------------------
# Straightforward, single-intent prompts
# ---------------------------------------------------------------------------

def test_straightforward_text_generation():
    assert classify_task("tell me a joke", None) == "text-generation"
    assert classify_task("what's the weather like today", None) == "text-generation"


def test_straightforward_code_execution():
    assert classify_task("execute this python script: print('hi')", None) == "code-execution"
    assert classify_task("run this code and tell me the output", None) == "code-execution"
    assert classify_task("write a program that reverses a string", None) == "code-execution"


def test_straightforward_document_generation():
    assert classify_task("generate a Word document for the quarterly summary", None) == "document-generation"
    assert classify_task("create a pptx presentation about safety training", None) == "document-generation"
    assert classify_task("make an excel spreadsheet of maintenance costs", None) == "document-generation"


def test_straightforward_doc_search():
    assert classify_task("find the procedure in the SOP", None) == "doc-search"
    assert classify_task("search the manual for valve specs", None) == "doc-search"


def test_straightforward_vision():
    assert classify_task("what is in this photo", "image/png") == "vision"
    assert classify_task("describe this image", "image/jpeg") == "vision"


# ---------------------------------------------------------------------------
# Uploaded images — deterministic, high-confidence, wins regardless of text
# ---------------------------------------------------------------------------

def test_image_upload_always_wins_regardless_of_prompt_wording():
    # Even prompt text that strongly suggests another task type must not
    # override an actual uploaded image.
    result = classify("generate a word document from this", "image/png")
    assert result.task_type == "vision"
    assert result.confidence == 1.0


def test_no_image_never_produces_vision():
    result = classify("what is in this photo", None)
    assert result.task_type != "vision"


# ---------------------------------------------------------------------------
# "calculate" must NOT alone force code-execution (the specific bug pattern
# the multi-signal rework targets) — but a real arithmetic expression, or a
# genuine code-execution phrase, still does.
# ---------------------------------------------------------------------------

def test_bare_calculate_does_not_force_code_execution():
    result = classify("please calculate the flow rate", None)
    assert result.task_type == "text-generation"
    assert result.scores["code-execution"] < 3  # weak signal only, below threshold


def test_calculate_with_arithmetic_expression_is_code_execution():
    assert classify_task("calculate 5 * 5", None) == "code-execution"
    assert classify_task("calculate 120 / 4", None) == "code-execution"


def test_calculate_with_domain_wording_stays_text_generation():
    # Domain-flavored "calculate" with no code fence, no arithmetic
    # expression, no "python"/"script"/"run this" — should not be dragged
    # into code-execution off one weak word.
    assert classify_task("calculate the corrosion rate for this pipeline", None) == "text-generation"
    assert classify_task("can you calculate the safety margin here", None) == "text-generation"


def test_code_fence_is_a_strong_code_execution_signal():
    assert classify_task("calculate the sum: ```python\nprint(2+2)\n```", None) == "code-execution"


# ---------------------------------------------------------------------------
# Ambiguous / weak prompts — must safely fall back to text-generation,
# never an aggressive classification off one weak keyword.
# ---------------------------------------------------------------------------

def test_single_weak_keyword_falls_back_to_text_generation():
    # "find" and "prepare" are both weak/ambiguous alone.
    assert classify_task("find a good name for this project", None) == "text-generation"
    assert classify_task("prepare yourself for the meeting", None) == "text-generation"
    assert classify_task("run a quick sanity check on your understanding", None) == "text-generation"


def test_ambiguity_result_carries_zero_confidence_when_falling_back():
    result = classify("tell me a joke", None)
    assert result.task_type == "text-generation"
    assert result.confidence == 0.0


# ---------------------------------------------------------------------------
# Conflicting-keyword prompts — should resolve to the strongest signal, not
# whichever keyword happens to be checked first.
# ---------------------------------------------------------------------------

def test_conflicting_keywords_resolves_to_strongest_signal():
    # Contains both a weak code-adjacent word ("calculate") and a strong,
    # explicit document-generation phrase — document-generation should win.
    result = classify("calculate the totals and generate a word document with them", None)
    assert result.task_type == "document-generation"
    assert result.scores["document-generation"] > result.scores["code-execution"]


# ---------------------------------------------------------------------------
# Multi-step prompts — primary task_type is still one of the 5 existing
# types (no invented type), but the classifier flags is_multi_step + an
# ordered workflow the planner can use.
# ---------------------------------------------------------------------------

def test_multi_step_doc_search_then_document_generation():
    prompt = (
        "Search the inspection reports for the leak in vessel B21, "
        "summarize the findings, and prepare an approval note as a Word document."
    )
    result = classify(prompt, None)
    assert result.task_type == "document-generation"  # final deliverable is the word doc
    assert result.is_multi_step is True
    assert result.workflow == ["doc-search", "document-generation"]


def test_single_step_document_generation_is_not_flagged_multi_step():
    result = classify("Generate a Word document for the approval note for leak in vessel B21.", None)
    assert result.task_type == "document-generation"
    assert result.is_multi_step is False
    assert result.workflow is None


# ---------------------------------------------------------------------------
# Industrial / domain-specific wording
# ---------------------------------------------------------------------------

def test_industrial_doc_search_wording():
    assert classify_task("search the sop for hot work permit rules", None) == "doc-search"
    assert classify_task("find the procedure for vessel entry", None) == "doc-search"


def test_industrial_document_generation_wording():
    assert classify_task("save it as a word document", None) == "document-generation"
    assert classify_task("generate an excel sheet with the readings", None) == "document-generation"


# ---------------------------------------------------------------------------
# Approval-note requests specifically — content-only (no file format) stays
# text-generation; naming a file format routes to document-generation.
# Model selection (LoRA adapter) is a separate decision in model_registry.py,
# not tested here.
# ---------------------------------------------------------------------------

def test_approval_note_without_file_format_is_text_generation():
    assert classify_task("make an approval note for the refinery pump inspection", None) == "text-generation"


def test_approval_note_with_file_format_is_document_generation():
    assert classify_task("generate a docx approval note", None) == "document-generation"
    assert classify_task(
        "Generate a Word document for the approval note for leak in vessel B21.", None
    ) == "document-generation"


# ---------------------------------------------------------------------------
# The two exact prompts called out in this round of work
# ---------------------------------------------------------------------------

def test_exact_prompt_generate_word_document_for_approval_note():
    result = classify("Generate a Word document for the approval note for leak in vessel B21.", None)
    assert result.task_type == "document-generation"
    # Classification only decides routing — it must never fabricate that the
    # incident's details are "known"; the result carries no invented content,
    # only the routing decision + matched signals.
    assert "vessel B21" not in str(result.matched_signals)


def test_exact_prompt_multi_step_search_then_approval_note():
    prompt = (
        "Search the inspection reports for the leak in vessel B21, "
        "summarize the findings, and prepare an approval note as a Word document."
    )
    result = classify(prompt, None)
    assert result.is_multi_step is True
    assert result.task_type == "document-generation"
    assert "doc-search" in result.workflow


# ---------------------------------------------------------------------------
# Word/Excel/PPT generation — each file-format family routes correctly
# ---------------------------------------------------------------------------

def test_word_excel_ppt_all_route_to_document_generation():
    assert classify_task("make a word doc summarizing today's shift", None) == "document-generation"
    assert classify_task("create an xlsx of the readings", None) == "document-generation"
    assert classify_task("make a pptx for the safety briefing", None) == "document-generation"


# ---------------------------------------------------------------------------
# Explainability: matched_signals/scores are populated and meaningful
# ---------------------------------------------------------------------------

def test_matched_signals_present_for_explainability():
    result = classify("generate a word document for the approval note", None)
    assert result.task_type in result.matched_signals
    assert len(result.matched_signals[result.task_type]) > 0
    assert result.scores["document-generation"] == result.confidence
