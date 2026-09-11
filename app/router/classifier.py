"""
Decides task_type from the incoming request.
task_type is a short descriptive string Person F owns per contract §2.4,
e.g. "text-generation", "vision", "code-execution",
     "document-generation", "doc-search".

Also exposes needs_reasoning() — used by the planner to decide whether a
vision task should chain into a second Qwen reasoning step, per the
image + reasoning -> Moondream -> observation -> Qwen -> final pipeline.

--- Multi-signal weighted classification (replaces plain substring matching) ---

The previous version of this file decided task_type by checking whether ANY
single keyword from a flat list appeared anywhere in the prompt. That is
fast and deterministic (good) but brittle for real industrial phrasing:
a single weak/ambiguous word (e.g. "calculate") could force a whole request
down the wrong path ("calculate the flow rate" is a plain question, not a
request to write and run code), and a file-format keyword sitting at the
FRONT of a sentence ("word doc of approval note...") could confuse anything
downstream that assumed it always trails the sentence.

This version keeps the same deterministic, keyword-based, no-model-call
approach (no Qwen/Ollama call is ever used just to classify — see
_score_task_type below) but scores multiple signals with different weights
instead of a single boolean "did any keyword match":

  - Strong, specific PHRASES (e.g. "generate a word document", "search the
    sop", "run this code") score much higher than a single generic word.
  - Weak/ambiguous single words (e.g. bare "calculate", bare "find") score
    low on their own — not enough by themselves to cross the classification
    threshold, but they still add up if several appear together, or combine
    with a genuinely strong signal (e.g. an actual arithmetic expression).
  - A task type must cross MIN_CONFIDENT_SCORE before it's accepted at all;
    otherwise the request safely falls back to "text-generation" rather than
    an aggressive guess from one weak keyword (ambiguity handling).
  - When more than one task type crosses that threshold AND the prompt
    contains a multi-step connector ("and then", "based on the findings",
    "search ... and prepare ...", etc.), the result is flagged as a
    multi-step request with an ordered `workflow` list — e.g. a request that
    both searches documents AND asks for a generated file is not "just"
    document-generation; it's doc-search feeding into document-generation.
    classify_task() (the existing string-returning function every current
    caller uses) still returns a single primary task_type unchanged in
    shape/contract — no new task_type string is invented — but the richer
    classify() function below exposes the full picture (scores, matched
    signals, is_multi_step, workflow) for the planner or logs to use.

Vision stays a fully deterministic, non-scored decision: an image
file_mime_type is always vision, full stop — no amount of scoring can ever
override an actual uploaded image, and no amount of prompt text can
manufacture a vision classification without one.
"""
import re
from dataclasses import dataclass, field
from typing import Optional

IMAGE_MIME_PREFIXES = ("image/",)

# A task type needs at least this much combined signal weight to be accepted
# at all. Below this, the signal is too weak/ambiguous to trust — see
# ambiguity handling in the module docstring above.
MIN_CONFIDENT_SCORE = 3

# File-format keywords — still exported as-is, since executor/planner.py's
# _strip_file_format_phrase() imports this exact name/shape and needs the
# flat keyword list, not the weighted signal table below.
FILE_FORMAT_KEYWORDS = (
    "docx", "pptx", "xlsx", "excel", "spreadsheet", "powerpoint", "presentation",
    "word doc", "word document", "word file",
    "generate a doc", "generate a document", "generate a file",
    "make a document", "make a file", "create a document", "create a file",
    "as a document", "as a file",
)

REASONING_KEYWORDS = (
    "why", "explain", "analyze", "analyse", "summarize", "summarise",
    "findings", "insight", "reasoning", "what does this mean",
    "assess", "evaluate", "recommend", "should", "risk", "issue",
    "approval note", "report",
)


# ---------------------------------------------------------------------------
# Signal tables: (phrase, weight, group) per task type.
#
# Groups exist purely for explainability/logging (item 9) — they don't
# change the score arithmetic, just label WHY a signal counts what it does
# when printed for debugging. Weight tiers used throughout:
#   5 = an unambiguous, explicit phrase naming exactly this task
#   3-4 = a strong, fairly specific term/phrase
#   1-2 = a weak or ambiguous word that means little on its own
# ---------------------------------------------------------------------------

CODE_EXECUTION_SIGNALS = (
    ("run this code", 5, "contextual_phrase"),
    ("execute this code", 5, "contextual_phrase"),
    ("run this script", 5, "contextual_phrase"),
    ("execute this script", 5, "contextual_phrase"),
    ("run this python", 5, "contextual_phrase"),
    ("write and run", 4, "contextual_phrase"),
    ("run the following", 3, "contextual_phrase"),
    ("write a program", 3, "contextual_phrase"),
    ("write a script", 3, "contextual_phrase"),
    # A plain "give/write me code to X" / "code for X" never says "run" or
    # "execute" at all, but is exactly as much a code-execution request as
    # "write a program that..." — without these, a real request like "give
    # me cpp code to reverse a linked list" scores 0 across every task
    # type, falls back to plain "text-generation", and (inside an ongoing
    # chat) gets silently upgraded to task_type="chat" by router.py —
    # which then grounds the answer in THIS CHAT'S Knowledge Base/history
    # instead of writing code, producing an answer about whatever else was
    # discussed earlier in the chat before it gets to the code. Observed
    # live: asking for this after a "summarize WW2" document-generation
    # turn in the same chat produced a stray WW2-grounded preamble before
    # the code. Fixed at the classification stage, not by special-casing
    # code requests inside the chat path — the request was never actually
    # a knowledge-base question to begin with.
    ("give me the code", 5, "contextual_phrase"),
    ("give me code", 5, "contextual_phrase"),
    ("code to ", 4, "contextual_phrase"),
    ("code for ", 4, "contextual_phrase"),
    ("write code", 4, "contextual_phrase"),
    ("write me code", 4, "contextual_phrase"),
    ("execute", 3, "action_verb"),
    ("python", 3, "tool_term"),
    ("c++", 3, "tool_term"),
    ("cpp", 3, "tool_term"),
    ("java", 3, "tool_term"),
    ("javascript", 3, "tool_term"),
    ("typescript", 3, "tool_term"),
    ("script", 2, "tool_term"),
    ("program", 2, "tool_term"),
    ("algorithm", 2, "tool_term"),
    ("function that", 2, "tool_term"),
    ("linked list", 2, "domain_term"),
    ("binary search", 2, "domain_term"),
    ("compute", 2, "action_verb"),
    ("implement", 1, "weak_domain_verb"),  # ambiguous alone ("implement the new policy")
    ("run", 1, "action_verb"),           # very ambiguous alone ("run a test")
    ("calculate", 1, "weak_domain_verb"),  # ambiguous alone — see module docstring
    ("computation", 1, "weak_domain_verb"),
)

DOC_SEARCH_SIGNALS = (
    ("search the sop", 5, "contextual_phrase"),
    ("search the manual", 5, "contextual_phrase"),
    ("find in the manual", 5, "contextual_phrase"),
    ("find the procedure", 5, "contextual_phrase"),
    ("look up in the sop", 5, "contextual_phrase"),
    ("find in docs", 4, "contextual_phrase"),
    ("search for", 2, "action_verb"),
    ("look up", 2, "action_verb"),
    ("search", 2, "action_verb"),
    ("find", 1, "action_verb"),          # weak alone ("find the leak" != doc-search)
    ("sop", 3, "domain_term"),
    ("sops", 3, "domain_term"),          # plural — \b-wrapped phrases don't stem, same pattern as
                                          # "inspection report"/"inspection reports" both being listed below
    ("manual", 2, "domain_term"),
    ("procedure", 2, "domain_term"),
    ("documentation", 2, "domain_term"),
    ("knowledge base", 2, "domain_term"),
    ("inspection report", 3, "domain_term"),
    ("inspection reports", 3, "domain_term"),
    ("inspection records", 2, "domain_term"),
    # Structured-record lookups against the local corpus (employee directory,
    # policy, asset register). These make "what is the employee ID of ...",
    # "what does POL-HR-04 say about ...", "when is V-204's next inspection
    # due" retrieve the actual record instead of falling through to a generic
    # text-generation answer.
    ("employee id", 5, "contextual_phrase"),
    ("emp id", 4, "contextual_phrase"),
    ("employee record", 4, "contextual_phrase"),
    ("employee directory", 5, "contextual_phrase"),
    ("reporting manager", 3, "domain_term"),
    ("date of joining", 3, "domain_term"),
    ("access level", 3, "domain_term"),
    ("asset register", 5, "contextual_phrase"),
    ("tag number", 3, "domain_term"),
    ("tag no", 3, "domain_term"),
    ("next inspection due", 4, "domain_term"),
    ("design pressure", 3, "domain_term"),
    ("design temp", 3, "domain_term"),
    ("custodian", 2, "domain_term"),
    ("policy", 2, "domain_term"),
    ("leave entitlement", 4, "contextual_phrase"),
    ("according to the", 2, "domain_term"),
    ("as per the", 2, "domain_term"),
)

DOCUMENT_GENERATION_SIGNALS = (
    ("generate a word document", 6, "contextual_phrase"),
    ("generate a docx", 6, "contextual_phrase"),
    ("make a word doc", 6, "contextual_phrase"),
    ("save it as a word document", 6, "contextual_phrase"),
    ("save as a word document", 6, "contextual_phrase"),
    ("create a presentation", 5, "contextual_phrase"),
    ("generate an excel sheet", 5, "contextual_phrase"),
    ("as a word document", 4, "output_term"),
    ("in word", 3, "output_term"),         # "prepare ... in Word" — narrow, deliberately scoped
    ("as a word", 3, "output_term"),
    ("docx", 4, "file_term"),
    ("pptx", 4, "file_term"),
    ("xlsx", 4, "file_term"),
    ("word document", 4, "file_term"),
    ("word doc", 4, "file_term"),
    ("word file", 4, "file_term"),
    ("excel", 3, "file_term"),
    ("spreadsheet", 3, "file_term"),
    ("powerpoint", 3, "file_term"),
    ("presentation", 3, "file_term"),
    ("generate a document", 3, "action_object"),
    ("generate a file", 3, "action_object"),
    ("make a document", 3, "action_object"),
    ("make a file", 3, "action_object"),
    ("create a document", 3, "action_object"),
    ("create a file", 3, "action_object"),
    ("as a document", 2, "output_term"),
    ("as a file", 2, "output_term"),
    ("prepare", 1, "weak_action_verb"),     # ambiguous alone ("prepare the pump for...")
    ("approval note", 1, "weak_domain_term"),  # ambiguous alone — see model_registry
)

IMAGE_GENERATION_SIGNALS = (
    ("generate an image", 6, "contextual_phrase"),
    ("generate an image of", 6, "contextual_phrase"),
    ("generate a picture", 6, "contextual_phrase"),
    ("draw a picture of", 6, "contextual_phrase"),
    ("create an image of", 6, "contextual_phrase"),
    ("make an image of", 6, "contextual_phrase"),
    ("draw an image of", 6, "contextual_phrase"),
    ("generate a photo", 5, "contextual_phrase"),
    ("draw me", 4, "contextual_phrase"),
    ("draw a", 3, "action_object"),
    ("paint a", 3, "action_object"),
    ("illustrate", 3, "action_verb"),
    ("image of", 3, "output_term"),
    ("picture of", 3, "output_term"),
    ("photo of", 2, "output_term"),
    # Diagram/chart phrasing — a request for a visual diagram of a process,
    # workflow, or structure is just as much an image-generation request as
    # "draw a picture of X"; without these, a perfectly common ask like
    # "create a diagram of the workflow" scored 0 on this task type.
    ("visual diagram", 5, "contextual_phrase"),
    ("diagram of", 5, "output_term"),
    ("flowchart of", 5, "output_term"),
    ("flow chart of", 5, "output_term"),
    ("chart of", 3, "output_term"),
    ("illustration of", 4, "output_term"),
    ("diagram", 3, "domain_term"),
    ("flowchart", 3, "domain_term"),
)

TIME_SERIES_FORECASTING_SIGNALS = (
    ("forecast the next", 6, "contextual_phrase"),
    ("predict the next", 5, "contextual_phrase"),
    ("forecast the following", 5, "contextual_phrase"),
    ("time series forecast", 6, "contextual_phrase"),
    ("time-series forecast", 6, "contextual_phrase"),
    ("forecast these values", 5, "contextual_phrase"),
    ("forecast this data", 5, "contextual_phrase"),
    ("forecast", 3, "action_verb"),
    ("predict", 1, "weak_action_verb"),  # ambiguous alone ("predict what will happen")
    ("time series", 3, "domain_term"),
    ("time-series", 3, "domain_term"),
    ("future values", 2, "domain_term"),
    ("next readings", 2, "domain_term"),
    ("trend projection", 3, "domain_term"),
)

# Multi-step connector phrases: signal that the prompt is describing a
# sequence of actions rather than one flat request. Used only to decide
# whether to flag is_multi_step / build an ordered workflow — never to
# invent a task type on its own.
MULTI_STEP_CONNECTORS = (
    "and then", "then prepare", "then generate", "then create", "then make",
    "after that", "based on the report", "based on the findings",
    "using the information", "and prepare", "and generate", "and create",
    "and make", "and summarize", "and summarise", "and write",
)

_ARITHMETIC_EXPRESSION_RE = re.compile(r"\d+\s*[\+\-\*/^]\s*\d+")
_CODE_FENCE_RE = re.compile(r"```")

# A request to PRODUCE numeric data / a computed sequence — the kind of
# thing a small language model routinely gets subtly wrong if it answers
# from its own head ("the first 20 Fibonacci numbers", "the 15th prime",
# "the sum of 1..100", "a multiplication table for 7"). Matching this
# routes the request through the code-execution flow instead: the agent
# writes a Python script, runs it in the sandbox, and answers ONLY from
# that verified output — no chance of a hallucinated value.
#
# Deliberately narrow and specific: it matches explicit, deterministically
# computable asks, NOT vague domain wording like "calculate the corrosion
# rate" (which has no sandbox-computable definition and must stay a plain
# text answer — see test_calculate_with_domain_wording_stays_text_generation).
_NUMERIC_TASK_RE = re.compile(
    r"""(?ix)
    \b(
        fibonacci
      | factorials?
      | primes\b | prime \s+ numbers?
      | perfect \s+ numbers?
      | triangular \s+ numbers?
      | (?: multiplication | times ) \s+ table
      | (?: first | last | next | top ) \s+ \d+ \s+ (?:\w+ \s+){0,3}
          (?: numbers? | terms? | values? | digits? | primes? | rows? | entries | elements? | integers? | multiples? )
      | \d+ (?: st | nd | rd | th ) \s+ (?:\w+ \s+){0,2}
          (?: number | term | prime | fibonacci | digit | row | value )
      | (?: sum | product | average | mean | median | mode | variance |
            standard \s+ deviation | std \s* dev(?:iation)? ) \s+ of \s
      | (?: gcd | lcm | hcf ) \s+ of \s
      | (?: factors | divisors | multiples ) \s+ of \s+ \d
      | (?: square | cube ) \s+ roots? \s+ of \s+ \d
      | sequence \s+ of \s+ \d
    )
    """,
)

# A document/record identifier from the local corpus: SOP-PTW-01, MAN-INSP-02,
# POL-HR-04, HR-DIR-01, ENG-AR-05, AN-2024-0417, EMP-1042, IT-CAT-2026, ...
_RECORD_ID_RE = re.compile(
    r"\b(?:sop|man|pol|hr|eng|it|an|emp|doc)-[a-z0-9]+(?:-[a-z0-9]+)*\b", re.IGNORECASE
)
# A plant equipment tag: V-101, V-204, P-220A, E-215, TK-330.
_EQUIPMENT_TAG_RE = re.compile(r"\b[a-z]{1,2}-\d{2,3}[a-z]?\b", re.IGNORECASE)
# "Question / lookup" framing — used to gate the equipment-tag boost so that
# "draft an approval note for the V-204 finding" (an action, not a lookup)
# is NOT pulled into doc-search, while "what is V-204's next inspection due?"
# is.
_LOOKUP_FRAMING_RE = re.compile(
    r"\b(what|which|who|whom|when|where|how many|how much|list|show|tell me|"
    r"look up|value|detail|details|specif\w*|state[sd]?|says?|mention\w*|"
    r"according to|as per|due|custodian|entitlement|email|id of|id for|"
    r"details of|details for|record for|record of)\b",
    re.IGNORECASE,
)


def _compile_phrase(phrase: str) -> re.Pattern:
    # \b...\b around the whole (possibly multi-word) phrase — this is what
    # fixes the old substring-matching false positives, e.g. old bare "sop"
    # matching inside "shop"/"stopped": \b anchors to real word boundaries.
    return re.compile(r"\b" + re.escape(phrase) + r"\b", re.IGNORECASE)


def _compiled(signals: tuple) -> tuple:
    return tuple((phrase, weight, group, _compile_phrase(phrase)) for phrase, weight, group in signals)


_COMPILED_SIGNALS = {
    "code-execution": _compiled(CODE_EXECUTION_SIGNALS),
    "doc-search": _compiled(DOC_SEARCH_SIGNALS),
    "document-generation": _compiled(DOCUMENT_GENERATION_SIGNALS),
    "image-generation": _compiled(IMAGE_GENERATION_SIGNALS),
    "time-series-forecasting": _compiled(TIME_SERIES_FORECASTING_SIGNALS),
}

# A real numeric series in the prompt (e.g. "10, 12, 14, 16, 18") is strong
# evidence of a forecasting request specifically — at least 3 numbers
# separated by commas/whitespace, distinct from a single arithmetic
# expression (_ARITHMETIC_EXPRESSION_RE, above) or a short list of IDs.
_NUMERIC_SERIES_RE = re.compile(r"(?:-?\d+(?:\.\d+)?\s*,\s*){2,}-?\d+(?:\.\d+)?")


@dataclass
class ClassificationResult:
    """
    Structured classification output — richer than the plain task_type
    string every existing caller uses. Nothing here changes any external
    contract (§2.4's response shape is untouched); this is purely internal,
    for the planner and for logs/debugging (item 9 — explainability without
    exposing chain-of-thought, since there isn't any: these are matched
    keyword signals, not model reasoning).
    """
    task_type: str
    confidence: float                      # winning task type's raw score (0 if fell back to default)
    scores: dict = field(default_factory=dict)         # {task_type: score} for every candidate considered
    matched_signals: dict = field(default_factory=dict)  # {task_type: [(phrase, weight, group), ...]}
    is_multi_step: bool = False
    workflow: Optional[list] = None        # ordered task_type sequence, e.g. ["doc-search", "document-generation"]


def _score_task_type(lowered_prompt: str, task_type: str) -> tuple:
    """Returns (score, [(phrase, weight, group, match_start_index), ...])."""
    total = 0.0
    matches = []
    for phrase, weight, group, pattern in _COMPILED_SIGNALS[task_type]:
        m = pattern.search(lowered_prompt)
        if m:
            total += weight
            matches.append((phrase, weight, group, m.start()))
    return total, matches


def classify(prompt: str, file_mime_type: Optional[str]) -> ClassificationResult:
    """
    Full multi-signal classification. classify_task() below is a thin
    backward-compatible wrapper around this for existing callers that just
    want the task_type string.
    """
    # Vision stays fully deterministic — an actual uploaded image always
    # wins, no scoring involved, and no prompt text alone can produce it.
    if file_mime_type and file_mime_type.startswith(IMAGE_MIME_PREFIXES):
        return ClassificationResult(
            task_type="vision", confidence=1.0,
            scores={"vision": 1.0}, matched_signals={"vision": [("<image upload>", 1.0, "file_mime_type")]},
        )

    lowered = prompt.lower()

    scores = {}
    matches_by_type = {}
    for task_type in (
        "code-execution", "doc-search", "document-generation",
        "image-generation", "time-series-forecasting",
    ):
        score, matches = _score_task_type(lowered, task_type)
        scores[task_type] = score
        matches_by_type[task_type] = matches

    # A real numeric series is strong, specific evidence of a forecasting
    # request — much stronger than the bare word "forecast" alone.
    if _NUMERIC_SERIES_RE.search(prompt):
        scores["time-series-forecasting"] += 4
        matches_by_type["time-series-forecasting"].append(("<numeric series>", 4, "numeric_evidence", 0))

    # Extra deterministic boosts that aren't simple phrase lookups:
    if _ARITHMETIC_EXPRESSION_RE.search(prompt):
        scores["code-execution"] += 3
        matches_by_type["code-execution"].append(("<arithmetic expression>", 3, "numeric_evidence", 0))
    if _CODE_FENCE_RE.search(prompt):
        scores["code-execution"] += 5
        matches_by_type["code-execution"].append(("<code fence>", 5, "numeric_evidence", 0))

    # An explicit "produce this computed number / sequence" ask routes to
    # code-execution so the value is computed + sandbox-verified rather than
    # answered from the model's head — UNLESS a file format was also named,
    # in which case document-generation stays primary and its own flow runs
    # the same verify-in-sandbox step before writing the file (see
    # app/agent/planner.py's _needs_computation / _filegen_codegen_step).
    if _NUMERIC_TASK_RE.search(prompt) and scores["document-generation"] < MIN_CONFIDENT_SCORE:
        scores["code-execution"] += 4
        matches_by_type["code-execution"].append(("<computed-number request>", 4, "numeric_evidence", 0))

    # A corpus document/record ID in the prompt ("what does SOP-PTW-01 say
    # about fire watch", "employee EMP-1042") is strong evidence of a
    # knowledge-base lookup.
    if _RECORD_ID_RE.search(prompt):
        scores["doc-search"] += 4
        matches_by_type["doc-search"].append(("<corpus record id>", 4, "identifier_evidence", 0))
    # An equipment tag only counts toward doc-search when the sentence is
    # framed as a question/lookup — never when it's an action request.
    if _EQUIPMENT_TAG_RE.search(prompt) and _LOOKUP_FRAMING_RE.search(prompt):
        scores["doc-search"] += 3
        matches_by_type["doc-search"].append(("<equipment tag lookup>", 3, "identifier_evidence", 0))

    qualifying = {t: s for t, s in scores.items() if s >= MIN_CONFIDENT_SCORE}

    has_connector = any(c in lowered for c in MULTI_STEP_CONNECTORS)
    is_multi_step = has_connector and len(qualifying) >= 2

    if not qualifying:
        # Ambiguity handling: no signal was strong enough to trust — never
        # make an aggressive call off one weak keyword.
        return ClassificationResult(
            task_type="text-generation", confidence=0.0,
            scores=scores, matched_signals=matches_by_type,
        )

    if is_multi_step:
        # Deliverable-oriented tie-break: if document-generation is one of
        # the qualifying types, it's the primary task_type — a request that
        # both searches documents AND asks for a generated file ultimately
        # produces a file, so document-generation is what the loop should
        # enter (per contract, task_type stays one of the 5 existing
        # strings — no new type invented). The full ordered workflow is
        # still exposed for the planner to use (or not) later.
        order = sorted(qualifying, key=lambda t: min(m[3] for m in matches_by_type[t]))
        primary = "document-generation" if "document-generation" in qualifying else order[0]
        return ClassificationResult(
            task_type=primary, confidence=qualifying[primary],
            scores=scores, matched_signals=matches_by_type,
            is_multi_step=True, workflow=order,
        )

    primary = max(qualifying, key=qualifying.get)
    return ClassificationResult(
        task_type=primary, confidence=qualifying[primary],
        scores=scores, matched_signals=matches_by_type,
    )


def classify_task(prompt: str, file_mime_type: str | None) -> str:
    """Backward-compatible: every existing caller just wants the task_type string."""
    return classify(prompt, file_mime_type).task_type


# ---------------------------------------------------------------------------
# Document-comparison / meeting-transcript intent detection.
#
# These do NOT create a new task_type — they are consumed by the router (to
# keep a comparison/transcript request inside a chat on the chat-KB flow
# instead of a corpus-wide doc-search) and by the planner (to shape the Qwen
# prompt into a structured diff / minutes). Keyword/regex only, no model call.
# ---------------------------------------------------------------------------

_COMPARISON_RE = re.compile(
    r"\b(compare|comparison|diff(?:erence)?s?|what'?s? different|reconcile|"
    r"inconsisten\w*|contradict\w*|discrepanc\w*|redline|red-line|delta|"
    r"versus|vs\.?)\b",
    re.IGNORECASE,
)
_TRANSCRIPT_RE = re.compile(
    r"\b(transcript|meeting notes|meeting minutes|minutes of (?:the )?meeting|"
    r"prepare (?:the )?minutes|\bmom\b|call notes|standup notes|action items|"
    r"decisions? (?:made|taken)|unresolved (?:issues|questions))\b",
    re.IGNORECASE,
)
_SPEAKER_LINE_RE = re.compile(r"^\s*[-*]?\s*[A-Z][\w .'-]{1,40}:\s+\S", re.MULTILINE)


def looks_like_comparison(text: str) -> bool:
    return bool(_COMPARISON_RE.search(text or ""))


def looks_like_transcript(text: str) -> bool:
    t = text or ""
    if _TRANSCRIPT_RE.search(t):
        return True
    return len(_SPEAKER_LINE_RE.findall(t)) >= 4


# Conversational filler — greetings, thanks, acknowledgements. These carry no
# question at all, so routing them down the chat/Knowledge-Base path (which is
# what any low-signal message in an active chat otherwise gets — see
# router.py) actively breaks them: the KB is searched using "hi" as the query,
# the prompt then instructs the model to answer FROM the Knowledge Base and to
# say so plainly when the passages don't cover it, and the recent-conversation
# block is right there for it to latch onto. Observed live: "hi" sent straight
# after a document-generation turn made the model re-emit that document's
# content instead of saying hello, and unrelated questions came back as "that
# isn't in the knowledge base". Matching is whole-message only (an exact match
# after stripping punctuation) so a real question that merely STARTS with
# "hi," or contains "thanks" — "thanks, now compare these two reports" — is
# never misread as filler.
_SMALLTALK_PHRASES = frozenset({
    "hi", "hii", "hey", "hello", "yo", "sup", "hiya", "howdy",
    "good morning", "good afternoon", "good evening", "good night",
    "thanks", "thank you", "thanks!", "ty", "thx", "cheers",
    "ok", "okay", "k", "cool", "nice", "great", "awesome", "perfect",
    "got it", "understood", "sounds good", "makes sense",
    "bye", "goodbye", "see ya", "see you", "later",
    "how are you", "how are you?", "whats up", "what's up",
    "who are you", "who are you?", "what can you do", "what can you do?",
})


def is_smalltalk(prompt: str) -> bool:
    """True only when the WHOLE message is conversational filler."""
    cleaned = (prompt or "").strip().lower().strip(".!?,；;… ")
    if not cleaned or len(cleaned) > 24:
        return False
    return cleaned in _SMALLTALK_PHRASES


def needs_reasoning(prompt: str, file_mime_type: str | None) -> bool:
    """
    True only for image tasks where the prompt implies analysis beyond a
    plain visual description — this is what triggers the second (Qwen)
    step in the agent loop after Moondream's observation.
    """
    if not (file_mime_type and file_mime_type.startswith(IMAGE_MIME_PREFIXES)):
        return False
    lowered = prompt.lower()
    return any(kw in lowered for kw in REASONING_KEYWORDS)
