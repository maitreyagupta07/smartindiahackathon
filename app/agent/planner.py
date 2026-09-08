"""
Real plan -> act -> observe -> replan decision logic.

decide_next_step() is called by executor/loop.py after EVERY step, not just
once — this is what makes the loop genuinely agentic rather than a fixed
pipeline: each call looks at the full state.step_records history so far
(persistent observation state) and decides what happens next.

Supported flows:
  1. text-generation
     -> call_qwen -> finalize

  2. vision, needs_reasoning == False (plain "what's in this image")
     -> call_moondream -> finalize

  3. vision, needs_reasoning == True (image + "explain/analyze/...")
     -> call_moondream (describe image)
        -> observe
        -> call_qwen (reason over Moondream's observation + original prompt)
        -> finalize

  4. code-execution
     -> call_qwen (generate a self-contained Python script that actually
                    solves the natural-language request — Person F never
                    hands Person C's execute_code the raw user prompt)
        -> observe generated code
        -> call_tool(execute_code)  (Person C — runs the Qwen-generated code)
           -> observe verified stdout/stderr/exit_code
           -> call_qwen (explain/verify the result against the original ask,
                          grounded in that verified output)
        -> finalize

     If Qwen's code-generation response contains no syntactically valid
     Python, Person F raises immediately rather than ever falling back to
     executing the raw natural-language prompt as "code".

  5. doc-search
     -> call_tool(search_docs)
        -> observe matched passages
        -> call_qwen (answer the original prompt grounded in those passages)
        -> finalize

  6. document-generation
     -> [if the request is computational/numerical]
          call_qwen (generate a Python script that computes the needed data)
            -> observe generated code
            -> call_tool(execute_code)  (Person C — verifies/computes the real data)
               -> observe verified stdout
               -> call_qwen (content-prep: turn original prompt + verified data
                              into structured FileContent JSON)
       [else]
          call_qwen (content-prep: turn original prompt directly into
                      structured FileContent JSON)
        -> observe structured content JSON
        -> call_tool(generate_file) using the PREPARED structured content
           (never the raw user prompt)
           -> observe file_url/file_name
           -> finalize

     Person F never hands Person C's FileContent schema a copy of the raw
     user prompt. A content-preparation stage (via Qwen, optionally grounded
     in verified execute_code output) always produces the {"title",
     "sections":[{"heading","body"}]} structure first.

Tool dispatch uses Person C's existing endpoints exactly as contracted in
§2.6, via clients/tools_client.py — no duplicate tool logic lives here,
only the decision of WHEN to call which tool and what to pass it.
"""
import json
import re
from dataclasses import dataclass
from typing import Literal, Optional

from .state import TaskState
from ..router.model_registry import TEXT_MODEL, VISION_MODEL, LORA_ADAPTER, is_approval_note_request
from ..router.classifier import (
    FILE_FORMAT_KEYWORDS,
    looks_like_comparison as _looks_like_comparison,
    looks_like_transcript as _looks_like_transcript,
)
from ..tools.pdf_extract import extract_text_from_pdf, is_pdf

Action = Literal["call_qwen", "call_moondream", "call_tool", "finalize"]
ToolName = Literal["execute_code", "search_docs", "generate_file", "scan_document"]

# A tool call is allowed at most this many total attempts (initial + retries)
# before the planner gives up and finalizes with an error. Independent of,
# and tighter than, state.max_steps — this bounds retries specifically.
MAX_TOOL_ATTEMPTS = 2

# How many times the filegen content-prep stage gets re-sampled after
# producing unparseable JSON before giving up and falling back to the
# generic raw-prompt content. This size of local model's strict-JSON output
# fails on a genuinely per-call basis (verified live: the identical prompt
# produced valid JSON on one attempt and malformed JSON on another) —
# retrying is a real recovery, not a guess.
MAX_FILEGEN_CONTENT_RETRIES = 1

# The approval-note LoRA adapter is a small, quantized model — at Ollama's
# default sampling temperature it occasionally goes fully off-script (e.g.
# a flat "I'm sorry, but I can't assist with that." on an entirely benign
# request, observed in testing). Approval notes need to be standardized/
# reliable output, not creative writing, so every LORA_ADAPTER call is
# dispatched at a lower, steadier temperature. Left as None (Ollama's
# default) for every other model — this is scoped to the adapter only.
LORA_TEMPERATURE = 0.2

# The document-generation codegen/content-prep stages ask for strict,
# machine-parseable output (runnable Python; a specific JSON schema) —
# not creative writing — and Ollama's own default temperature (~0.8) was
# observed live to produce genuinely malformed JSON often enough to
# matter (a stray unquoted key, mismatched quote escaping) even when the
# model otherwise understood the task correctly. A lower, steadier
# temperature measurably reduces that failure mode, the same reasoning
# LORA_TEMPERATURE already uses for the approval-note adapter.
FILEGEN_STRUCTURED_TEMPERATURE = 0.3

# Internal stage markers prefixed onto call_qwen prompts during the
# document-generation flow so decide_next_step can tell, purely from
# state.step_records (no extra mutable flow-control state), which stage of
# that flow a completed Qwen call belongs to. Never shown to the user —
# stripped before the prompt is actually sent to Qwen.
FILEGEN_CODE_MARKER = "__FILEGEN_CODE__"
FILEGEN_CONTENT_MARKER = "__FILEGEN_CONTENT__"

# The plain code-execution flow's codegen stage reuses FILEGEN_CODE_MARKER's
# exact string value on purpose, rather than introducing a third marker:
# executor/loop.py strips a marker prefix off a prompt before it ever
# reaches Qwen by comparing against the literal FILEGEN_CODE_MARKER /
# FILEGEN_CONTENT_MARKER values it imports by name, and this module cannot
# teach it a new marker without editing loop.py. state.task_type — already
# set before decide_next_step ever runs — is what actually disambiguates
# "this is the code-execution codegen stage" from "this is the
# document-generation codegen stage" at the two call sites that check it.
CODEEXEC_CODE_MARKER = FILEGEN_CODE_MARKER


# Heuristic keywords indicating the file-generation request needs real
# computed/verified data rather than free-form prose — in which case F
# should run execute_code first and ground the file content in its output.
_COMPUTE_KEYWORDS = (
    "calculate", "compute", "computation", "sum", "average", "mean", "median",
    "total", "fibonacci", "prime", "factorial", "sequence", "statistics",
    "count", "sort", "series", "numeric", "numbers",
)

# Signal for "the image is a document to be TRANSCRIBED, not a scene to be
# DESCRIBED" — a request an uploaded image can still satisfy, but via OCR
# (app/tools/ocr.py) rather than Moondream. Deliberately kept separate from
# vision classification proper (an image mime type always sets task_type
# ="vision" — see classifier.py's docstring on that being non-negotiable);
# this only decides which of the TWO vision-capable paths handles the step0
# entry point once task_type is already "vision".
_DOCUMENT_SCAN_KEYWORDS = (
    "transcribe", "transcription", "handwritten", "handwriting",
    "read this note", "read the note", "read this document",
    "what does this note say", "what does this say", "digitize",
    "ocr", "scan this document", "scan this note", "scan this page",
    "extract the text", "extract text",
)


def _is_document_scan_request(prompt: str) -> bool:
    lowered = (prompt or "").lower()
    return any(kw in lowered for kw in _DOCUMENT_SCAN_KEYWORDS)


class CodeGenerationError(Exception):
    """
    Raised when Qwen's code-execution codegen stage does not return usable
    Python. Deliberately left uncaught here — it propagates out of
    decide_next_step to executor/loop.py's outer exception handler, which
    turns any uncaught exception into a clean status="failed" response.
    Neither retrying execute_code with the same unusable text nor silently
    substituting the raw natural-language user prompt is an acceptable
    fallback, so surfacing this as a hard failure is the correct behavior.
    """


@dataclass
class NextStep:
    action: Action
    model: Optional[str] = None
    prompt: Optional[str] = None
    image_base64: Optional[str] = None
    tool_name: Optional[ToolName] = None
    tool_args: Optional[dict] = None
    temperature: Optional[float] = None


# ---------------------------------------------------------------------------
# Helpers: building tool arguments / follow-up prompts from state
# ---------------------------------------------------------------------------

def _extract_code(prompt: str) -> str:
    """
    Pull code out of `prompt` if it's fenced in a ``` code block; otherwise
    fall back to treating the whole string as the code to run. Only ever
    call this on a Qwen code-generation response, never on the raw
    natural-language user prompt — Qwen's response is expected to actually
    be Python; a natural-language user prompt is not, and running it through
    the sandbox as-is produces a SyntaxError, not a useful result.
    """
    if "```" in prompt:
        parts = prompt.split("```")
        if len(parts) >= 2:
            block = parts[1]
            # strip an optional leading language tag, e.g. ```python
            first_line, _, rest = block.partition("\n")
            if rest and first_line.strip().isalpha():
                return rest.strip()
            return block.strip()
    return prompt.strip()


_FILE_TYPE_KEYWORDS = {
    "xlsx": ("xlsx", "excel", "spreadsheet"),
    "pptx": ("pptx", "powerpoint", "slide", "presentation"),
    "docx": ("docx", "word doc", "word document", "word file"),
}


def _detect_requested_file_types(prompt: str) -> list[str]:
    """
    Every distinct file format actually named in the prompt, in the order
    they FIRST appear (position, not a fixed xlsx > pptx > docx priority —
    that priority order used to mean "...in Word doc, then...Excel..."
    silently produced an xlsx because "excel" happened to be checked
    first, regardless of which format the user actually asked for first).
    Two or more entries here means a genuinely multi-deliverable request —
    see _detect_file_type's docstring for why that matters.
    """
    lowered = prompt.lower()
    found = [(lowered.index(kw), fmt) for fmt, kws in _FILE_TYPE_KEYWORDS.items()
             for kw in kws if kw in lowered]
    seen, ordered = set(), []
    for _, fmt in sorted(found):
        if fmt not in seen:
            seen.add(fmt)
            ordered.append(fmt)
    return ordered


def _detect_file_type(prompt: str) -> str:
    types = _detect_requested_file_types(prompt)
    return types[0] if types else "docx"  # default per problem statement's approval-note use case


_SEQUENCE_CONNECTOR_RE = re.compile(r"\s*(?:,\s*)?\b(?:and then|then|after that|afterwards|next)\b\s*", re.IGNORECASE)


def _split_multi_deliverable_prompt(prompt: str, file_types: list[str]) -> dict[str, str]:
    """
    Best-effort split of a multi-deliverable prompt ("...word doc on X then
    excel of Y...") into one text fragment per deliverable, using common
    sequencing connectors ("then", "and then", "after that", ...).

    This exists because telling the model "you're only doing X right now,
    ignore the Y part" was NOT reliable on this size of local model —
    observed live, across repeated attempts: it sometimes still included
    the other topic's content anyway, and after a more forceful version of
    that instruction, sometimes overcorrected into producing NO content at
    all ({"title": "", "sections": []}). Splitting the prompt so each
    deliverable's content-prep call is only ever shown ITS OWN fragment —
    never even sees the other deliverable's text — sidesteps needing the
    model to selectively ignore part of what it's given, for the common
    "X in FORMAT1, then Y in FORMAT2" phrasing pattern.

    Returns {} (never partial) when the split doesn't cleanly produce
    exactly one fragment naming exactly one of each requested type —
    ambiguous/unusual phrasing — so callers can fall back to the
    whole-prompt + explicit-instruction approach instead.
    """
    if len(file_types) < 2:
        return {}
    fragments = [f.strip() for f in _SEQUENCE_CONNECTOR_RE.split(prompt) if f.strip()]
    if len(fragments) < 2:
        return {}
    mapping: dict[str, str] = {}
    for frag in fragments:
        types_in_frag = _detect_requested_file_types(frag)
        if len(types_in_frag) == 1 and types_in_frag[0] not in mapping:
            mapping[types_in_frag[0]] = frag
    return mapping if set(mapping) == set(file_types) else {}


def _scoped_deliverable_prompt(state: "TaskState") -> tuple[str, bool]:
    """
    Returns (prompt_text_to_send_to_qwen, was_cleanly_split) for the
    deliverable state is CURRENTLY producing (state.file_type). When the
    split succeeds, prompt_text_to_send_to_qwen is JUST that deliverable's
    own fragment — the model is never shown the other deliverable's text
    at all. When it doesn't (single-deliverable request, or phrasing the
    splitter can't cleanly parse), falls back to the full original prompt.
    """
    if len(state.file_types) < 2:
        return state.prompt, False
    mapping = _split_multi_deliverable_prompt(state.prompt, state.file_types)
    if state.file_type in mapping:
        return mapping[state.file_type], True
    return state.prompt, False


def _build_generate_file_args(prompt: str, state: Optional[TaskState] = None) -> dict:
    """
    Last-resort fallback content when the content-prep JSON stage couldn't
    be parsed at all — dumps the raw request as a single "Details" section
    rather than failing the whole task. When state is a multi-deliverable
    request, uses just this deliverable's own text fragment (when the
    prompt cleanly split — see _scoped_deliverable_prompt) so even this
    fallback doesn't dump the OTHER deliverable's request text into a file
    that has nothing to do with it.
    """
    file_type = state.file_type if state is not None and state.file_type else _detect_file_type(prompt)
    if state is not None:
        prompt, _ = _scoped_deliverable_prompt(state)
    title = prompt.strip().splitlines()[0][:80] or "Generated Document"
    content = {
        "title": title,
        "sections": [
            {"heading": "Details", "body": prompt.strip()},
        ],
    }
    return {"file_type": file_type, "content": content}


def _filegen_model(prompt: str) -> str:
    """
    Approval-note-flavored requests use Person A's fine-tuned LORA_ADAPTER
    (trained specifically on approval-note phrasing/structure) for every
    Qwen call in the document-generation flow; everything else keeps using
    the base TEXT_MODEL. Falls back to TEXT_MODEL if the adapter isn't
    configured yet. Note: whether this request is a document-generation task
    at all (i.e. whether a file actually gets produced) is decided
    separately by the router's classifier — this only picks which model to
    use once we're already in that flow.
    """
    if is_approval_note_request(prompt) and LORA_ADAPTER:
        return LORA_ADAPTER
    return TEXT_MODEL


def _text_model(prompt: str) -> str:
    """
    Same adapter choice as _filegen_model, but for the plain text-generation
    entry point — an approval-note-style request that never mentioned a file
    format (e.g. "make an approval note for the refinery") should still get
    the LoRA adapter's writing style, it just won't produce a file.
    """
    if is_approval_note_request(prompt) and LORA_ADAPTER:
        return LORA_ADAPTER
    return TEXT_MODEL


_COMPUTE_KEYWORD_RE = re.compile(
    r"\b(" + "|".join(re.escape(kw) for kw in _COMPUTE_KEYWORDS) + r")\b"
)


def _needs_computation(prompt: str) -> bool:
    """
    Heuristic: does this file-generation request depend on real computed/
    numeric data (e.g. "first 20 Fibonacci numbers ... average") rather than
    free-form prose? If so, F should verify the numbers via execute_code
    before preparing file content, instead of trusting Qwen's arithmetic.

    Word-boundary matched, not a bare substring check — "sum" as a plain
    substring matches inside "summer", so a request like "summarize the
    summer season" used to false-positive trigger a whole spurious
    codegen -> execute_code -> content-prep chain for a request with
    nothing to compute at all (observed live). Same risk existed for
    "run"/"count" (⊂ "running"/"discount", etc.).
    """
    return bool(_COMPUTE_KEYWORD_RE.search(prompt.lower()))


def _build_filegen_code_prompt(original_prompt: str, state: Optional[TaskState] = None) -> str:
    """
    Asks Qwen to produce a runnable Python script (executed via Person C's
    execute_code tool) that computes whatever data the file-generation
    request needs, printing it as JSON so the next stage can ground the
    file content in verified output rather than model arithmetic.
    """
    if state is not None:
        original_prompt, _ = _scoped_deliverable_prompt(state)
    return (
        "You are preparing verified data for a document/spreadsheet generation request.\n"
        f"User request: \"{original_prompt}\"\n\n"
        "Write ONLY a single self-contained Python script (no markdown fences, no "
        "explanation, nothing but code) that computes whatever data the request "
        "needs and prints the final result as JSON to stdout via "
        "`print(json.dumps(result))`. Include every individual item/entry the "
        "request asks for (e.g. all N values, not just a sample) plus any "
        "requested aggregates (sum, average, etc)."
    )


def _build_codeexec_code_prompt(original_prompt: str) -> str:
    """
    Asks Qwen to produce a complete, directly-runnable Python program
    (executed via Person C's execute_code tool) that solves an arbitrary
    natural-language code-execution request. Deliberately generic — it names
    no specific task, calculation, variable, library, or output shape, so
    the exact same prompt template works for any request. The user's prompt
    itself is natural language, not Python, and must never be sent to
    execute_code directly.
    """
    return (
        "You are a Python code generator. Given the user's request below, "
        "write a complete Python program that actually performs the "
        "requested computation or action — do not just describe, explain, "
        "or outline how it could be done.\n\n"
        f"User request: \"{original_prompt}\"\n\n"
        "Requirements:\n"
        "- Write the full program, not a fragment. Include every import it "
        "needs, explicitly. Do not assume any module, variable, function, "
        "file, or other state already exists — the program starts from a "
        "completely fresh Python process with no memory of anything before it.\n"
        "- Prefer the Python standard library. Only reach for a third-party "
        "package if the request genuinely cannot be satisfied without one.\n"
        "- The program must run to completion entirely on its own: no manual "
        "edits, no placeholders/TODOs, and no interactive input.\n"
        "- The program must print the result(s) of the computation/action to "
        "stdout in a clear, readable form, so it can be explained afterward.\n"
        "- Respond with ONLY the code, ideally inside a single ```python "
        "code block, and nothing else — no explanation, commentary, or "
        "pseudocode before or after it."
    )


def _is_usable_python(code: str) -> bool:
    """
    Best-effort check that a Qwen code-generation response actually is
    Python source — not empty, not a refusal, not stray prose — before it is
    ever handed to Person C's execute_code sandbox. Uses compile() purely to
    validate syntax; never executes/evals the untrusted text.
    """
    if not code or not code.strip():
        return False
    try:
        compile(code, "<qwen-generated>", "exec")
        return True
    except SyntaxError:
        return False


def _build_filegen_content_prompt(
    original_prompt: str, verified_data: Optional[str], state: Optional[TaskState] = None
) -> str:
    """
    Asks Qwen to turn the user's request (optionally grounded in verified
    execute_code output) into Person C's FileContent shape — as plain
    TITLE:/"## Heading" text (_parse_markdown_content), NOT JSON.

    This used to ask for a raw JSON object matching the FileContent
    schema directly. Repeatedly observed live: this size/quantization of
    model has a genuinely per-call, probabilistic failure rate at
    strict-JSON syntax (unquoted keys, duplicate keys, trailing commas,
    JS-style comments, runaway repetition mid-object) that survived
    several rounds of prompt tightening, a JSON repair pass, and a retry.
    Markdown headings have no comparable failure mode — there is no
    bracket/quote balancing to get wrong, "## Heading" is either present
    on a line or it isn't, and small instruct models are extensively
    trained on exactly this format. The parser (_parse_markdown_content)
    is correspondingly simpler than the JSON path it replaced.
    """
    # A prompt like "...in word doc then make an excel report of..." names
    # TWO different deliverables in one message; run_agent_loop now DOES
    # produce both (see _filegen_entry_step / state.file_types), one at a
    # time, in order. Preferred approach: split the prompt so this call
    # only ever SEES its own deliverable's text (_scoped_deliverable_prompt)
    # — the model can't leak the other topic in if it was never shown it.
    # Fallback approach (split didn't cleanly parse the phrasing): send the
    # full prompt with an explicit "ignore the other part" instruction —
    # observed live to be less reliable on its own (sometimes still
    # included the other topic; a more forceful version of the instruction
    # sometimes overcorrected into empty content instead), so it's kept
    # only as a safety net, not the primary mechanism.
    was_split = False
    if state is not None:
        original_prompt, was_split = _scoped_deliverable_prompt(state)

    data_block = (
        f"Verified computed data (use these exact values — do not recompute, "
        f"alter, or shorten them):\n{verified_data}\n\n"
        if verified_data else ""
    )
    if _looks_like_transcript(original_prompt):
        guidance_block = f"Follow this structure — one section per item:\n{_transcript_instructions()}\n\n"
    elif _looks_like_comparison(original_prompt):
        guidance_block = f"Follow this structure — one section per numbered item:\n{_comparison_instructions()}\n\n"
    else:
        guidance_block = ""
    if state is not None and len(state.file_types) > 1 and not was_split:
        chosen = state.file_type
        already_done = [t for i, t in enumerate(state.file_types) if i < state.file_index]
        still_pending = [t for i, t in enumerate(state.file_types) if i > state.file_index]
        other_notes = []
        if already_done:
            other_notes.append(f"already generated separately: {', '.join(t.upper() for t in already_done)}")
        if still_pending:
            other_notes.append(f"will be generated separately right after this one: {', '.join(t.upper() for t in still_pending)}")
        scope_block = (
            f"IMPORTANT: this request names MULTIPLE separate deliverables. You are producing "
            f"ONLY the {chosen.upper()} file right now ({'; '.join(other_notes)}). Produce sections "
            f"for the {chosen.upper()} topic ONLY. Do NOT create a section for any other "
            f"deliverable — not even a short one. If a value for another deliverable isn't a "
            f"real fact from the {chosen.upper()} topic itself, leave it out entirely rather than "
            f"invent or guess at it.\n\n"
        )
    else:
        scope_block = ""
    return (
        "You are preparing content for a generated file.\n"
        f"User request: \"{original_prompt}\"\n\n"
        f"{scope_block}"
        f"{guidance_block}"
        f"{data_block}"
        "Respond with PLAIN TEXT ONLY, in exactly this format (no JSON, no code "
        "fences, no commentary outside it):\n\n"
        "TITLE: <a short title>\n\n"
        "## <heading of the first section>\n"
        "<body text for this section — can be multiple lines/paragraphs, and may use "
        "newlines to lay out a list/table as plain text, one item per line>\n\n"
        "## <heading of the next section, only if genuinely needed>\n"
        "<body text>\n\n"
        "The content must fully represent what the user actually requested — include "
        "EVERY requested item/entry (not a summary and not the request text itself). "
        "Use multiple \"## \" sections only to separate genuinely different topics (e.g. "
        "one section for the data, another for a requested summary/average) — NEVER "
        "create one section per individual item in a single list (e.g. do not make a "
        "separate section for each number if asked for a list of numbers — list them all "
        "in ONE section's body instead)."
    )


_TITLE_LINE_RE = re.compile(r"(?im)^\s*TITLE:\s*(.+?)\s*$")
_MARKDOWN_HEADING_RE = re.compile(r"(?m)^#{1,6}[ \t]+(.+?)[ \t]*$")


def _stringify_content_value(value):
    """
    A value that isn't already a plain string (e.g. a list of numbers
    straight out of a JSON-decoded execute_code result — see
    _build_content_from_verified_data) becomes one NEWLINE-joined string,
    one item per line — NOT json.dumps(value). filegen.py's docx/xlsx/pptx
    writers all split a section's body on "\n" to lay out one paragraph/
    row/bullet per line; json.dumps would produce a single-line JSON-
    array-literal string instead (an Excel cell literally containing the
    text '["1", "2", "3", ...]' instead of one number per row) — valid as
    a string, but not remotely what "formatted" means for a spreadsheet.
    Each item is also stripped, since a list item can carry its own stray
    leading/trailing whitespace.
    """
    if isinstance(value, list):
        return "\n".join(str(v).strip() for v in value)
    if isinstance(value, dict):
        return "\n".join(f"{k}: {v}" for k, v in value.items())
    return str(value)


def _parse_markdown_content(text: str) -> Optional[dict]:
    """
    Parses the TITLE: / "## Heading" plain-text format
    _build_filegen_content_prompt now asks for, into the same
    {"title", "sections":[{"heading","body"}]} shape the JSON schema this
    replaced used to produce.

    This is deliberately NOT a JSON parser. A markdown heading is either
    present at the start of a line or it isn't — there is no bracket/quote
    balancing, no escaping, no trailing-comma or duplicate-key failure
    mode to have. Repeated live testing of the JSON-based approach this
    replaced showed a genuinely per-call, probabilistic syntax failure
    rate on this size/quantization of model that several rounds of
    prompt-tightening, a repair pass, and a retry could reduce but never
    close; markdown headings don't have that failure surface to begin
    with, so there's structurally less for the model to get wrong.
    """
    if not text or not text.strip():
        return None
    text = text.strip()
    if "```" in text:
        text = text.replace("```", "")

    title = "Generated Document"
    title_match = _TITLE_LINE_RE.search(text)
    if title_match:
        title = title_match.group(1).strip()
        text = text[title_match.end():].strip()

    pieces = _MARKDOWN_HEADING_RE.split(text)
    sections = []
    # pieces = [text-before-first-heading, heading1, body1, heading2, body2, ...]
    if pieces and pieces[0].strip():
        sections.append({"heading": "", "body": pieces[0].strip()})
    for i in range(1, len(pieces), 2):
        heading = pieces[i].strip()
        body = pieces[i + 1].strip() if i + 1 < len(pieces) else ""
        if heading or body:
            sections.append({"heading": heading, "body": body})
    if not sections and text:
        sections = [{"heading": "", "body": text}]
    if not sections:
        return None

    for s in sections:
        s["heading"] = strip_markdown_emphasis(s["heading"])
        s["body"] = strip_markdown_emphasis(s["body"])
    return {"title": strip_markdown_emphasis(title), "sections": sections}


def _humanize_key(key: str) -> str:
    return key.replace("_", " ").replace("-", " ").strip().title() or "Data"


def _build_content_from_verified_data(prompt: str, stdout: str) -> Optional[dict]:
    """
    Builds the FileContent structure DIRECTLY from execute_code's own
    verified stdout, in plain code — no LLM call involved at all.

    _build_filegen_code_prompt already asks Qwen's generated Python to
    `print(json.dumps(result))`, and that JSON comes out of a REAL Python
    interpreter, not free-form LLM text — json.dumps() is always
    syntactically valid JSON. Asking Qwen a SECOND time to re-transcribe
    that same already-reliable data into another hand-written JSON/text
    response was a pure loss: it added another chance to fail (observed
    live: hallucinated wrong numbers despite the real values already being
    available) for data that never needed touching by a model again once
    it was correctly computed. This is "give the data, let a tool build
    the JSON" applied directly — skips the unreliable step instead of
    trying to make it more reliable.

    Returns None (caller falls back to the normal LLM content-prep call)
    when stdout isn't parseable JSON, or is empty/scalar-only — not
    every computation result is a clean list/dict worth building a whole
    file section from without any model involvement.
    """
    try:
        data = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        return None

    title = prompt.strip().splitlines()[0][:80] or "Generated Document"
    sections = []
    if isinstance(data, dict) and data:
        for key, value in data.items():
            sections.append({"heading": _humanize_key(str(key)), "body": _stringify_content_value(value)})
    elif isinstance(data, list) and data:
        sections.append({"heading": "Data", "body": _stringify_content_value(data)})
    else:
        return None
    return {"title": title, "sections": sections}


def _build_lora_prompt(state: TaskState, base_prompt: str) -> str:
    """
    If an uploaded PDF came with this approval-note request (a real or
    scanned inspection report — contract §2.4's file_base64/file_mime_type),
    ground the adapter's prompt in the report's actual extracted text
    instead of writing a generic note out of thin air. Extraction is only
    ever attempted here, for an approval-note request — a PDF attached to
    an unrelated request is left completely alone (see PERSON_A_NOTES.md).
    Falls back to the plain prompt if there's no file, it's not a PDF, or
    nothing could be extracted from it (e.g. a scanned PDF on a machine
    without tesseract-ocr installed) — never blocks the request on this.
    """
    if not (state.file_base64 and is_pdf(state.file_mime_type)):
        return base_prompt
    extracted = extract_text_from_pdf(state.file_base64)
    if not extracted:
        print(
            f"[PLANNER] task_id={state.task_id} PDF uploaded but no text could be "
            f"extracted (scanned page + no OCR available?) -> using prompt only"
        )
        return base_prompt
    print(
        f"[PLANNER] task_id={state.task_id} using extracted PDF text "
        f"({len(extracted)} chars) as approval-note grounding"
    )
    return f"{base_prompt}\n\nInspection report content:\n{extracted}"


_FILE_FORMAT_TRAILING_FILLER_RE = re.compile(r"^\s*(of|for|about|as a|as an|in)\b", re.IGNORECASE)
_FILE_FORMAT_LEADING_FILLER_RE = re.compile(r"\b(as a|as an|in)\s*$", re.IGNORECASE)


def _strip_file_format_phrase(prompt: str) -> str:
    """
    Removes a file-format ask (e.g. "save it as a word document", or
    "word doc" sitting at the FRONT of the sentence — e.g. "make a word doc
    of approval note of leak in vessel B2") from the user's prompt before it
    reaches the approval-note LoRA adapter. The adapter was trained only on
    plain finding-description phrasing (never asked to "save as a
    document"), so document-generation and text-generation requests for the
    same underlying request must reach it with the same core wording —
    otherwise the unfamiliar file-format phrasing drifts it into
    conversational preamble/postamble instead of its trained format.

    Earlier versions of this function found the FIRST matching keyword and
    cut everything from there to the end of the string — correct only when
    the file-format phrase trails the sentence ("...as a word document").
    When it sits at the front or middle instead, that truncated away the
    actual content along with it. This version removes only the matched
    phrase itself (plus one leftover connecting word right after it, e.g.
    "of"/"for"), wherever in the sentence it sits, leaving the rest intact.
    """
    lowered = prompt.lower()
    # Longest-first: FILE_FORMAT_KEYWORDS has overlapping entries (e.g.
    # "word doc" is a literal substring of "word document") — matching the
    # shortest one first would chop only part of the actual phrase and
    # leave a stray fragment ("...as a ument") behind.
    for kw in sorted(FILE_FORMAT_KEYWORDS, key=len, reverse=True):
        idx = lowered.find(kw)
        if idx == -1:
            continue
        before = prompt[:idx]
        after = prompt[idx + len(kw):]
        # Drop one leftover connector immediately after the phrase (e.g.
        # "word doc OF approval note...") and immediately before it (e.g.
        # "save it AS A word document") — whichever side it fell on.
        after = _FILE_FORMAT_TRAILING_FILLER_RE.sub("", after, count=1).lstrip()
        before = _FILE_FORMAT_LEADING_FILLER_RE.sub("", before).rstrip(" ,.-—")
        # Keep sentence-ending punctuation attached without an extra space.
        joiner = "" if (after and after[0] in ",.!?") else (" " if after else "")
        result = (before + joiner + after).strip(" ,")
        return result or prompt
    return prompt


def strip_markdown_emphasis(text: str) -> str:
    """
    The approval-note LoRA adapter (and, observed live, the base model's
    filegen content-prep stage too) sometimes writes markdown-style
    emphasis/headings (**bold**, __bold__, "# Heading") into its output,
    but nothing downstream renders markdown — not the plain-text API
    response, and not the docx/xlsx/pptx writers, which just write
    characters literally. Left alone, that means literal asterisks and
    "#" characters show up in both the text answer and the generated
    file (visible as "**Date:**" instead of an actually bold "Date:", or
    a stray "# Overview" line instead of a real heading — filegen.py's
    writers already apply REAL heading styling via their own APIs
    wherever a section's `heading` field is used, so a leading "#" in
    the text itself is always redundant, never needed). Strip it here,
    in code, right after the model call, rather than asking the model
    not to use markdown (an instruction a small model won't reliably
    follow).
    """
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"__(.+?)__", r"\1", text)
    text = text.replace("**", "").replace("__", "")
    return re.sub(r"(?m)^#{1,6}[ \t]+", "", text)


def _approval_note_text_to_file_content(raw_text: str) -> dict:
    """
    Converts the LoRA adapter's own trained free-text approval-note format
    (e.g. "APPROVAL NOTE\n\nSubject: ...\n\nFindings:\n...\n\nRecommendation:
    \n...\n\nApproval Status: ...") into the {"title","sections"} shape
    generate_file needs — without asking the model to reinvent the
    structure as JSON. The adapter was never trained to emit JSON, so
    re-prompting it for that schema pushes it off-distribution and produces
    inconsistent results; parsing its real, trained-format output in code
    instead keeps the docx in the exact same approval-note structure
    (Subject / Date / Findings / Recommendation / Approval Status /
    Signature, etc.) as the plain-text answer for the same kind of request.
    """
    raw_text = strip_markdown_emphasis(raw_text)
    blocks = [b.strip() for b in raw_text.strip().split("\n\n") if b.strip()]
    if not blocks:
        return {"title": "Approval Note", "sections": [{"heading": "", "body": raw_text.strip()}]}

    title = blocks[0].splitlines()[0].strip() or "Approval Note"
    sections = []
    for block in blocks[1:]:
        lines = block.splitlines()
        first = lines[0].strip()
        if first.endswith(":") and len(lines) > 1:
            heading, body = first[:-1].strip(), "\n".join(lines[1:]).strip()
        elif ":" in first and len(lines) == 1 and len(first) < 60:
            heading, body = (p.strip() for p in first.split(":", 1))
        else:
            heading, body = "", block
        sections.append({"heading": heading, "body": body})

    if not sections:
        sections = [{"heading": "", "body": raw_text.strip()}]
    return {"title": title, "sections": sections}


def _is_valid_file_content(obj) -> bool:
    return (
        isinstance(obj, dict)
        and isinstance(obj.get("title"), str)
        and isinstance(obj.get("sections"), list)
        and len(obj["sections"]) > 0
        and all(
            isinstance(s, dict) and isinstance(s.get("heading"), str) and isinstance(s.get("body"), str)
            for s in obj["sections"]
        )
    )


def _build_reasoning_prompt(original_prompt: str, moondream_observation: str) -> str:
    """
    Combines the user's original request with Moondream's raw image
    description into a single prompt for the Qwen reasoning step.
    """
    return (
        f"An image was analyzed and described as follows:\n"
        f"---\n{moondream_observation}\n---\n\n"
        f"Based on that description, respond to the user's original request:\n"
        f"\"{original_prompt}\""
    )


def _build_ocr_result_prompt(original_prompt: str, tool_observation: dict) -> str:
    """
    Passes Tesseract's raw transcription to Qwen for cleanup/answering —
    Qwen is explicitly told this is unverified raw OCR (never scene
    description) so it can flag garbled output honestly instead of
    presenting noisy OCR as a confident, clean transcription.
    """
    if not tool_observation.get("available", True):
        return (
            f"The user asked to scan/transcribe an uploaded image (\"{original_prompt}\"), "
            f"but OCR is not available on this machine (tesseract-ocr is not installed). "
            f"Tell the user this plainly — do not guess at what the document might say."
        )
    text = (tool_observation.get("text") or "").strip()
    if not text:
        return (
            f"The user asked to scan/transcribe an uploaded image (\"{original_prompt}\"), "
            f"but OCR found no readable text in it (the image may be blank, too blurry, or "
            f"not actually text). Tell the user that plainly — do not invent content."
        )
    return (
        f"The user asked to scan/transcribe an uploaded image: \"{original_prompt}\"\n\n"
        f"Raw, UNVERIFIED OCR output from that image (Tesseract, not a language model — "
        f"it may contain misread characters, especially for cursive handwriting):\n"
        f"---\n{text}\n---\n\n"
        f"Present this to the user as the transcription, lightly cleaning up obvious OCR "
        f"noise (stray characters, broken line breaks) WITHOUT changing the actual wording "
        f"or inventing words that aren't recognizable in the raw text above. If large parts "
        f"look too garbled to trust, say so honestly instead of presenting a confident guess."
    )


def _build_code_result_prompt(original_prompt: str, tool_observation: dict) -> str:
    stdout = tool_observation.get("stdout", "")
    stderr = tool_observation.get("stderr", "")
    exit_code = tool_observation.get("exit_code")

    if exit_code != 0:
        # The sandbox run failed (e.g. a syntax error because the prompt had
        # no actual code for Person F to extract) — Qwen must report the
        # failure honestly, never invent a plausible-looking "verified"
        # answer on top of a run that didn't actually succeed.
        return (
            f"Code was executed to satisfy this request: \"{original_prompt}\"\n\n"
            f"exit_code: {exit_code}\n"
            f"stdout:\n{stdout}\n"
            f"stderr:\n{stderr}\n\n"
            f"The execution FAILED (non-zero exit code). Do NOT invent or guess a result. "
            f"Tell the user the code execution failed and include the relevant error from stderr."
        )

    return (
        f"The following code was executed to satisfy this request: \"{original_prompt}\"\n\n"
        f"exit_code: {exit_code}\n"
        f"stdout:\n{stdout}\n"
        f"stderr:\n{stderr}\n\n"
        f"Using this VERIFIED execution result (never re-derive the arithmetic yourself), "
        f"write a short answer to the original request for the user."
    )


def _build_docsearch_prompt(original_prompt: str, tool_observation: dict) -> str:
    results = tool_observation.get("results", [])
    if not results:
        return (
            f"A local knowledge-base search for this request returned no matching "
            f"passages: \"{original_prompt}\"\n\n"
            f"Reply with exactly: \"That information is not in the knowledge base.\" "
            f"Do not guess or use outside knowledge."
        )

    passages = "\n\n".join(
        f"[Passage {i}] (source: {r.get('source', 'unknown')}"
        + (f", page {r['page']}" if r.get("page") else "")
        + f")\n{r.get('text', '')}"
        for i, r in enumerate(results, start=1)
    )
    return (
        "You are answering strictly from the plant knowledge base. Use ONLY the "
        "passages below — do not add outside knowledge, do not estimate, and do "
        "not fill gaps.\n\n"
        f"QUESTION: \"{original_prompt}\"\n\n"
        f"RETRIEVED PASSAGES:\n---\n{passages}\n---\n\n"
        "Instructions:\n"
        "- Answer directly and specifically. If the question asks for a value, ID, "
        "date, name, or rule, quote it exactly as written in the passages.\n"
        "- Name the source document your answer comes from (e.g. \"per "
        "SOP-PTW-01\" or \"from employee_directory.txt\").\n"
        "- If the passages do not contain the answer, reply exactly: \"That "
        "information is not in the knowledge base.\" — nothing more."
    )


def _format_history(history: Optional[list]) -> str:
    """Render recent conversation turns as `User:` / `Assistant:` lines."""
    lines = []
    for msg in history or []:
        if not isinstance(msg, dict):
            continue
        role = "Assistant" if msg.get("role") == "assistant" else "User"
        content = str(msg.get("content", "")).strip()
        if content:
            lines.append(f"{role}: {content}")
    return "\n".join(lines).strip()


# ---------------------------------------------------------------------------
# Document comparison + meeting-transcript recognition (multi-doc / transcript
# workflows). These do not introduce a new task_type — a comparison request
# runs through the normal chat / doc-search / document-generation flow; these
# helpers just let the planner shape the Qwen prompt for the specific job so
# the answer is a structured diff / a set of minutes rather than a vague
# summary. Everything stays grounded in retrieved passages or the pasted text.
# ---------------------------------------------------------------------------

def _comparison_instructions() -> str:
    return (
        "This is a DOCUMENT COMPARISON request. Work only from the material "
        "provided (retrieved passages and/or pasted text). Produce these "
        "sections, each as a short list; write \"None found\" if a section is "
        "empty:\n"
        "1. Changed values / clauses — show old -> new.\n"
        "2. Additions — present in one document, absent in the other.\n"
        "3. Removals — dropped from the later document.\n"
        "4. Contradictions or inconsistencies between the documents.\n"
        "5. Possible operational / business impact of these differences.\n"
        "6. Recommended follow-up actions.\n"
        "Name the documents you are comparing. Do not invent differences that "
        "the provided text does not support."
    )


def _transcript_instructions() -> str:
    return (
        "This is a MEETING-TRANSCRIPT processing request. Work only from the "
        "transcript text provided. Produce these sections:\n"
        "- Attendees — only if named in the transcript, else \"Not stated\".\n"
        "- Summary — 3-6 bullets of what was discussed.\n"
        "- Decisions — each decision on its own line, else \"None recorded\".\n"
        "- Action Items — one per line as: owner - action - due date (use "
        "\"unassigned\" / \"no date\" when the transcript does not say).\n"
        "- Unresolved Issues — open questions with no decision, else \"None\".\n"
        "Do not add commitments, owners, or dates that are not in the transcript."
    )


def _build_transcript_prompt(original_prompt: str) -> str:
    return (
        f"{_transcript_instructions()}\n\n"
        f"REQUEST / TRANSCRIPT:\n\"{original_prompt}\""
    )


def _build_comparison_prompt(original_prompt: str) -> str:
    return (
        f"{_comparison_instructions()}\n\n"
        f"REQUEST / DOCUMENTS:\n\"{original_prompt}\""
    )


def _build_chat_prompt(question: str, history: Optional[list], tool_observation: dict) -> str:
    """
    Assembles the chat-flow prompt for Qwen:

        SYSTEM instructions
        RELEVANT KNOWLEDGE BASE   (chat-scoped retrieved chunks, with source/page)
        RECENT CONVERSATION       (last few User/Assistant turns)
        CURRENT QUESTION

    So a follow-up like "why would they benefit from it?" can resolve "they"
    and "it" against the earlier turns, grounded in this chat's own uploads.
    """
    results = (tool_observation or {}).get("results", []) or []
    if results:
        kb_block = "\n\n".join(
            "[{src}{page}]\n{body}".format(
                src=r.get("source", "document"),
                page=f", page {r['page']}" if r.get("page") else "",
                body=r.get("text", ""),
            )
            for r in results
        )
    else:
        kb_block = "(no relevant passages found in this chat's uploaded documents)"

    convo = _format_history(history) or "(no earlier conversation in this chat)"

    if _looks_like_transcript(question):
        task_block = f"\n\nTASK GUIDANCE:\n{_transcript_instructions()}"
    elif _looks_like_comparison(question):
        task_block = f"\n\nTASK GUIDANCE:\n{_comparison_instructions()}"
    else:
        task_block = ""

    return (
        "You are an assistant answering questions using the user's uploaded "
        "Knowledge Base and the conversation so far in this chat. Use the "
        "recent conversation to resolve references such as \"it\", \"they\", "
        "\"this\", or \"that\". Prefer the Knowledge Base passages for facts; "
        "if they do not contain the answer, say so plainly instead of "
        "guessing."
        f"{task_block}\n\n"
        f"RELEVANT KNOWLEDGE BASE:\n{kb_block}\n\n"
        f"RECENT CONVERSATION:\n{convo}\n\n"
        f"CURRENT QUESTION:\n{question}"
    )


def _sources_from_results(tool_observation: dict) -> list[dict]:
    """De-duplicated [{filename, page, score}] for the chat response's `sources`."""
    seen: set = set()
    out: list[dict] = []
    for r in (tool_observation or {}).get("results", []) or []:
        key = (r.get("source"), r.get("page"))
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "filename": r.get("source"),
            "page": r.get("page"),
            "score": r.get("score"),
        })
    return out


def _tool_attempt_count(state: TaskState, tool_name: str) -> int:
    return sum(
        1 for r in state.step_records
        if r.action == "call_tool" and r.tool_name == tool_name
    )


def _last_execute_code_stdout(state: TaskState) -> Optional[str]:
    """
    The most recent successful execute_code observation's stdout, if any —
    used to re-ground a content-prep RETRY in the same verified data the
    original (failed-to-parse) attempt had, instead of silently losing that
    grounding just because this attempt didn't need to re-run the
    computation itself.
    """
    for r in reversed(state.step_records):
        if r.action == "call_tool" and r.tool_name == "execute_code" and r.status == "ok":
            return (r.observation or {}).get("stdout")
    return None


def _filegen_entry_step(state: TaskState) -> NextStep:
    """
    Decides the first Qwen call for producing state.file_type — the SAME
    decision whether this is the very first deliverable (called from step 0)
    or a later one in a multi-deliverable request like "...word doc on X
    then excel of Y..." (called again from the "generate_file observed"
    branch below, once state.file_index has moved to the next file_type).
    Factored out so both call sites make this decision identically instead
    of drifting out of sync.
    """
    filegen_model = _filegen_model(state.prompt)
    # Scoped to THIS deliverable's own text when the request cleanly split
    # (see _scoped_deliverable_prompt) — checking the full combined prompt
    # here means a request like "...word doc on winters in egypt then
    # excel of first 10 numbers..." sees "numbers" and spuriously runs a
    # whole codegen -> execute_code chain for the ESSAY deliverable too,
    # which has nothing to compute at all. Observed live: the model wrote
    # nonsense Python (an `import requests` "fetch data" attempt) trying
    # to satisfy that spurious instruction, which then fed a garbled
    # content-prep stage and fell back to raw-prompt content.
    computation_scope_prompt, _ = _scoped_deliverable_prompt(state)
    if _needs_computation(computation_scope_prompt):
        print(
            f"[PLANNER] task_id={state.task_id} filegen[{state.file_index}]={state.file_type} "
            f"-> call_qwen model={filegen_model} (generate verification code, computation detected)"
        )
        return NextStep(
            action="call_qwen",
            model=filegen_model,
            prompt=FILEGEN_CODE_MARKER + _build_filegen_code_prompt(state.prompt, state=state),
            temperature=FILEGEN_STRUCTURED_TEMPERATURE,
        )
    if filegen_model == LORA_ADAPTER:
        # The adapter is trained to write approval notes directly in its
        # own free-text format, not Person C's generic FileContent JSON —
        # asking it for JSON here would push it off the distribution it
        # was trained on. Call it plainly, as trained, and turn its real
        # output into sections in code afterwards (see
        # _approval_note_text_to_file_content) instead of re-prompting it
        # to invent a schema.
        print(
            f"[PLANNER] task_id={state.task_id} filegen[{state.file_index}]={state.file_type} "
            f"-> call_qwen model={LORA_ADAPTER} (approval-note LoRA, trained free-text format)"
        )
        core_prompt = _strip_file_format_phrase(state.prompt)
        return NextStep(
            action="call_qwen",
            model=LORA_ADAPTER,
            prompt=FILEGEN_CONTENT_MARKER + _build_lora_prompt(state, core_prompt),
            temperature=LORA_TEMPERATURE,
        )
    print(
        f"[PLANNER] task_id={state.task_id} filegen[{state.file_index}]={state.file_type} "
        f"-> call_qwen model={filegen_model} (content-preparation stage)"
    )
    return NextStep(
        action="call_qwen",
        model=filegen_model,
        prompt=FILEGEN_CONTENT_MARKER + _build_filegen_content_prompt(state.prompt, None, state=state),
        temperature=FILEGEN_STRUCTURED_TEMPERATURE,
    )


# ---------------------------------------------------------------------------
# Core planner
# ---------------------------------------------------------------------------

def decide_next_step(state: TaskState) -> NextStep:
    """
    Core replanning decision. Called once per loop iteration in
    executor/loop.py, AFTER observing the previous step's result.
    """
    # Max-step protection — hard stop regardless of task_type, so a bug in
    # this function's logic can never spin the loop forever.
    if state.hit_max_steps():
        print(f"[PLANNER] task_id={state.task_id} max_steps reached ({state.max_steps}) -> forcing finalize")
        return NextStep(action="finalize")

    n = state.step_count  # steps already executed so far

    # --- Step 0: nothing executed yet -> decide the entry point ---
    if n == 0:
        if state.task_type == "vision":
            if _is_document_scan_request(state.prompt):
                print(f"[PLANNER] task_id={state.task_id} step0 -> call_tool(scan_document) (handwritten/document scan request)")
                return NextStep(
                    action="call_tool",
                    tool_name="scan_document",
                    tool_args={"image_base64": state.file_base64},
                )
            print(f"[PLANNER] task_id={state.task_id} step0 -> call_moondream (vision entry point)")
            return NextStep(
                action="call_moondream",
                model=VISION_MODEL,
                prompt=state.prompt,
                image_base64=state.file_base64,
            )

        if state.task_type == "code-execution":
            print(
                f"[PLANNER] task_id={state.task_id} step0 -> call_qwen "
                f"(codeexec: generate Python code before execution — never the raw prompt)"
            )
            return NextStep(
                action="call_qwen",
                model=TEXT_MODEL,
                prompt=CODEEXEC_CODE_MARKER + _build_codeexec_code_prompt(state.prompt),
            )

        if state.task_type == "doc-search":
            # Comparison questions need chunks from more than one document, so
            # widen retrieval for them; a plain fact lookup stays tight.
            top_k = 8 if _looks_like_comparison(state.prompt) else 5
            print(f"[PLANNER] task_id={state.task_id} step0 -> call_tool(search_docs) top_k={top_k}")
            return NextStep(
                action="call_tool",
                tool_name="search_docs",
                tool_args={"query": state.prompt, "top_k": top_k},
            )

        if state.task_type == "chat":
            # Chat flow: retrieve from THIS chat's own uploads AND the
            # operator's persistent global Knowledge Base (both isolation
            # filters enforced in docsearch.search_all), then answer with
            # the retrieved chunks + recent conversation context.
            chat_top_k = 8 if (
                _looks_like_comparison(state.prompt) or _looks_like_transcript(state.prompt)
            ) else 5
            print(
                f"[PLANNER] task_id={state.task_id} step0 -> call_tool(search_docs) "
                f"chat_id={state.chat_id!r} user_id={state.user_id!r} top_k={chat_top_k} "
                f"(chat uploads + operator global KB retrieval)"
            )
            return NextStep(
                action="call_tool",
                tool_name="search_docs",
                tool_args={
                    "query": state.prompt,
                    "top_k": chat_top_k,
                    "chat_id": state.chat_id,
                    "user_id": state.user_id,
                },
            )

        if state.task_type == "document-generation":
            # Every distinct file format actually named in the prompt, in
            # the order requested — see _filegen_entry_step's docstring for
            # how state.file_index steps through these one at a time.
            state.file_types = _detect_requested_file_types(state.prompt) or ["docx"]
            state.file_index = 0
            state.file_type = state.file_types[0]
            # Each deliverable can take up to 5 steps on its own (codegen ->
            # execute_code -> content-prep -> one content-prep RETRY on
            # invalid JSON (MAX_FILEGEN_CONTENT_RETRIES) -> generate_file)
            # — the single-deliverable default (MAX_STEPS_DEFAULT) doesn't
            # leave enough room for a second (or third) deliverable's full
            # chain in the same task. The +2 slack (matching the default's
            # own cushion over its single-deliverable worst case) is NOT
            # optional headroom — hit_max_steps() is checked BEFORE a
            # step's own result is processed, so with zero slack the final
            # generate_file's own success is never actually observed: the
            # loop force-finalizes right on top of it instead of recording
            # it, silently dropping the last deliverable (observed live:
            # steps showed a successful 2nd generate_file call, but
            # state.generated_files only ever had 1 entry).
            state.max_steps = max(state.max_steps, (4 + MAX_FILEGEN_CONTENT_RETRIES) * len(state.file_types) + 2)
            return _filegen_entry_step(state)

        # text-generation (default).
        # An approval-note-flavored request uses the same LoRA adapter as
        # the document-generation flow, called the same trained way (plain
        # prompt, no forced JSON schema) — so a text-only answer is in the
        # exact same approval-note format (Subject / Findings /
        # Recommendation / Approval Status / ...) that a docx of the same
        # kind of request would carry, just delivered as plain text instead
        # of written to a file. Anything else keeps the original single
        # free-form Qwen call.
        # A pasted meeting transcript or an inline document-comparison request
        # (no chat_id, so it can't go through the chat-KB flow) is shaped here
        # so the single Qwen call returns structured minutes / a structured
        # diff instead of loose prose. Still one plain call_qwen — no new
        # task_type, no extra tool.
        if _looks_like_transcript(state.prompt):
            print(f"[PLANNER] task_id={state.task_id} step0 -> call_qwen model={TEXT_MODEL} (transcript processing)")
            return NextStep(action="call_qwen", model=TEXT_MODEL, prompt=_build_transcript_prompt(state.prompt))
        if _looks_like_comparison(state.prompt):
            print(f"[PLANNER] task_id={state.task_id} step0 -> call_qwen model={TEXT_MODEL} (inline document comparison)")
            return NextStep(action="call_qwen", model=TEXT_MODEL, prompt=_build_comparison_prompt(state.prompt))

        if is_approval_note_request(state.prompt) and LORA_ADAPTER:
            print(
                f"[PLANNER] task_id={state.task_id} step0 -> call_qwen model={LORA_ADAPTER} "
                f"(text entry point: approval-note LoRA adapter, called in its trained format)"
            )
            return NextStep(
                action="call_qwen",
                model=LORA_ADAPTER,
                prompt=_build_lora_prompt(state, state.prompt),
                temperature=LORA_TEMPERATURE,
            )

        print(f"[PLANNER] task_id={state.task_id} step0 -> call_qwen model={TEXT_MODEL} (text entry point)")
        return NextStep(action="call_qwen", model=TEXT_MODEL, prompt=state.prompt)

    # --- Step 1+: replan based on what happened last ---
    last = state.step_records[-1]

    if last.status == "error":
        # Tool calls get a bounded retry; model calls and repeated tool
        # failures finalize with the error surfaced to the caller.
        if last.action == "call_tool" and last.tool_name:
            attempts = _tool_attempt_count(state, last.tool_name)
            if attempts < MAX_TOOL_ATTEMPTS:
                print(
                    f"[PLANNER] task_id={state.task_id} tool '{last.tool_name}' failed "
                    f"(attempt {attempts}/{MAX_TOOL_ATTEMPTS}) -> retrying same tool call"
                )
                return NextStep(
                    action="call_tool",
                    tool_name=last.tool_name,
                    tool_args=_rebuild_tool_args(state, last),
                )
            print(
                f"[PLANNER] task_id={state.task_id} tool '{last.tool_name}' failed "
                f"{attempts}x -> giving up, finalize with error"
            )
            return NextStep(action="finalize")

        print(f"[PLANNER] task_id={state.task_id} last step errored -> finalize")
        return NextStep(action="finalize")

    if last.action == "call_moondream":
        if state.needs_reasoning:
            # image + reasoning -> Moondream -> observation -> Qwen -> final
            reasoning_prompt = _build_reasoning_prompt(state.prompt, str(last.observation))
            print(f"[PLANNER] task_id={state.task_id} moondream observed -> chaining to call_qwen for reasoning")
            return NextStep(action="call_qwen", model=TEXT_MODEL, prompt=reasoning_prompt)
        print(f"[PLANNER] task_id={state.task_id} moondream observed, no reasoning needed -> finalize")
        return NextStep(action="finalize")

    if last.action == "call_qwen":
        if state.task_type == "code-execution" and (last.prompt_used or "").startswith(CODEEXEC_CODE_MARKER):
            code = _extract_code(str(last.observation))
            if not _is_usable_python(code):
                print(
                    f"[PLANNER] task_id={state.task_id} codeexec code-generation produced no "
                    f"usable Python -> raising (never falling back to the raw user prompt)"
                )
                raise CodeGenerationError(
                    "Qwen did not return a usable Python script for this code-execution request."
                )
            print(
                f"[PLANNER] task_id={state.task_id} codeexec code generated "
                f"-> call_tool(execute_code) to run the Qwen-generated code"
            )
            return NextStep(
                action="call_tool",
                tool_name="execute_code",
                tool_args={"code": code, "language": "python"},
            )

        if state.task_type == "document-generation" and (last.prompt_used or "").startswith(FILEGEN_CODE_MARKER):
            code = _extract_code(str(last.observation))
            print(
                f"[PLANNER] task_id={state.task_id} filegen verification code generated "
                f"-> call_tool(execute_code) to compute real data before content-prep"
            )
            return NextStep(
                action="call_tool",
                tool_name="execute_code",
                tool_args={"code": code, "language": "python"},
            )

        if state.task_type == "document-generation" and (last.prompt_used or "").startswith(FILEGEN_CONTENT_MARKER):
            if last.model_used == LORA_ADAPTER:
                # Adapter output is its own trained free-text approval-note
                # format — parse it in code (see
                # _approval_note_text_to_file_content) instead of trying to
                # decode it as the generic filegen content shape.
                content = _approval_note_text_to_file_content(str(last.observation))
            else:
                content = _parse_markdown_content(str(last.observation))
                if not _is_valid_file_content(content):
                    if state.filegen_content_retries < MAX_FILEGEN_CONTENT_RETRIES:
                        state.filegen_content_retries += 1
                        print(
                            f"[PLANNER] task_id={state.task_id} filegen content-prep returned unparseable content "
                            f"-> retrying ({state.filegen_content_retries}/{MAX_FILEGEN_CONTENT_RETRIES})"
                        )
                        return NextStep(
                            action="call_qwen",
                            model=_filegen_model(state.prompt),
                            prompt=FILEGEN_CONTENT_MARKER + _build_filegen_content_prompt(
                                state.prompt, _last_execute_code_stdout(state), state=state
                            ),
                            temperature=FILEGEN_STRUCTURED_TEMPERATURE,
                        )
                    print(
                        f"[PLANNER] task_id={state.task_id} filegen content-prep returned unparseable content "
                        f"after {state.filegen_content_retries} retry(ies) -> falling back to raw-prompt content"
                    )
                    content = _build_generate_file_args(state.prompt, state=state)["content"]
            state.prepared_file_content = content
            file_type = state.file_type or _detect_file_type(state.prompt)
            print(
                f"[PLANNER] task_id={state.task_id} filegen content prepared "
                f"(title={content.get('title')!r}, sections={len(content.get('sections', []))}) "
                f"-> call_tool(generate_file) file_type={file_type}"
            )
            return NextStep(
                action="call_tool",
                tool_name="generate_file",
                tool_args={"file_type": file_type, "content": content},
            )

        # Whether this was the plain text entry point or a post-tool /
        # post-Moondream reasoning step, a completed Qwen call is terminal.
        print(f"[PLANNER] task_id={state.task_id} qwen observed -> finalize")
        return NextStep(action="finalize")

    if last.action == "call_tool":
        if last.tool_name == "execute_code":
            if state.task_type == "document-generation":
                stdout = (last.observation or {}).get("stdout", "")
                filegen_model = _filegen_model(state.prompt)

                # Skip the LLM content-prep call entirely when the verified
                # stdout already IS clean, structured data — building the
                # file content from it directly in code is strictly more
                # reliable than asking Qwen to re-transcribe the same
                # already-correct numbers into another response (see
                # _build_content_from_verified_data's docstring). Not
                # attempted for the approval-note LoRA path, which needs
                # its own trained narrative style, not a raw data dump.
                if filegen_model != LORA_ADAPTER:
                    scoped_prompt, _ = _scoped_deliverable_prompt(state)
                    deterministic_content = _build_content_from_verified_data(scoped_prompt, stdout)
                    if deterministic_content is not None:
                        state.prepared_file_content = deterministic_content
                        print(
                            f"[PLANNER] task_id={state.task_id} filegen content built directly from "
                            f"verified execute_code data (no extra model call) "
                            f"(title={deterministic_content.get('title')!r}, "
                            f"sections={len(deterministic_content.get('sections', []))}) "
                            f"-> call_tool(generate_file) file_type={state.file_type}"
                        )
                        return NextStep(
                            action="call_tool",
                            tool_name="generate_file",
                            tool_args={"file_type": state.file_type, "content": deterministic_content},
                        )

                print(
                    f"[PLANNER] task_id={state.task_id} filegen verification code executed "
                    f"-> chaining to call_qwen model={filegen_model} "
                    f"(content-prep stage, grounded in verified stdout"
                    f"{', using approval-note LoRA adapter' if filegen_model == LORA_ADAPTER else ''})"
                )
                return NextStep(
                    action="call_qwen",
                    model=filegen_model,
                    prompt=FILEGEN_CONTENT_MARKER + _build_filegen_content_prompt(state.prompt, stdout, state=state),
                    temperature=LORA_TEMPERATURE if filegen_model == LORA_ADAPTER else FILEGEN_STRUCTURED_TEMPERATURE,
                )
            reasoning_prompt = _build_code_result_prompt(state.prompt, last.observation or {})
            print(f"[PLANNER] task_id={state.task_id} execute_code observed -> chaining to call_qwen")
            return NextStep(action="call_qwen", model=TEXT_MODEL, prompt=reasoning_prompt)

        if last.tool_name == "search_docs":
            if state.task_type == "chat":
                state.sources = _sources_from_results(last.observation or {})
                chat_prompt = _build_chat_prompt(state.prompt, state.history, last.observation or {})
                print(
                    f"[PLANNER] task_id={state.task_id} chat KB retrieval observed "
                    f"({len(state.sources)} source(s)) -> chaining to call_qwen with conversation context"
                )
                return NextStep(action="call_qwen", model=TEXT_MODEL, prompt=chat_prompt)
            # Expose which corpus document(s) grounded the answer so the UI's
            # existing "Sources" block can show them (same shape the chat flow
            # already uses).
            state.sources = _sources_from_results(last.observation or {})
            reasoning_prompt = _build_docsearch_prompt(state.prompt, last.observation or {})
            print(
                f"[PLANNER] task_id={state.task_id} search_docs observed "
                f"({len(state.sources)} source(s)) -> chaining to call_qwen"
            )
            return NextStep(action="call_qwen", model=TEXT_MODEL, prompt=reasoning_prompt)

        if last.tool_name == "scan_document":
            reasoning_prompt = _build_ocr_result_prompt(state.prompt, last.observation or {})
            print(f"[PLANNER] task_id={state.task_id} scan_document observed -> chaining to call_qwen")
            return NextStep(action="call_qwen", model=TEXT_MODEL, prompt=reasoning_prompt)

        if last.tool_name == "generate_file":
            # File is the deliverable itself — do not let Qwen describe it,
            # just record it. state.file_url/file_name (singular, back-compat
            # with every existing caller/test) always reflect the FIRST file
            # generated; state.generated_files accumulates every one, in
            # order, for the additive multi-file response field.
            file_obs = last.observation or {}
            generated = {"file_url": file_obs.get("file_url"), "file_name": file_obs.get("file_name")}
            state.generated_files.append(generated)
            if state.file_url is None:
                state.file_url = generated["file_url"]
                state.file_name = generated["file_name"]
            print(
                f"[PLANNER] task_id={state.task_id} generate_file observed "
                f"[{state.file_index + 1}/{len(state.file_types) or 1}] "
                f"file_url={generated['file_url']} file_name={generated['file_name']}"
            )

            state.file_index += 1
            if state.file_index < len(state.file_types):
                # More deliverables were named in the same request (e.g.
                # "...word doc on X then excel of Y...") — move on to the
                # next one and restart its own content-prep from scratch,
                # exactly like the first deliverable did at step 0.
                state.file_type = state.file_types[state.file_index]
                state.prepared_file_content = None
                state.filegen_content_retries = 0
                print(
                    f"[PLANNER] task_id={state.task_id} -> starting next deliverable "
                    f"[{state.file_index + 1}/{len(state.file_types)}]={state.file_type}"
                )
                return _filegen_entry_step(state)

            print(f"[PLANNER] task_id={state.task_id} all requested deliverable(s) generated -> finalize")
            return NextStep(action="finalize")

        print(f"[PLANNER] task_id={state.task_id} unknown tool_name '{last.tool_name}' -> finalize")
        return NextStep(action="finalize")

    # Any other/unhandled action falls through to finalize defensively.
    print(f"[PLANNER] task_id={state.task_id} unhandled last action '{last.action}' -> finalize")
    return NextStep(action="finalize")


def _rebuild_tool_args(state: TaskState, failed_step) -> dict:
    """
    Rebuilds the same tool call's args for a retry. Prefers replaying the
    exact args the failed attempt used (e.g. Qwen-generated code, or
    already-prepared file content) — recomputing from state.prompt is only
    a defensive fallback for a step that somehow recorded no tool_args.
    """
    if failed_step.tool_args:
        return failed_step.tool_args
    if failed_step.tool_name == "execute_code":
        return {"code": _extract_code(state.prompt), "language": "python"}
    if failed_step.tool_name == "search_docs":
        args = {"query": state.prompt, "top_k": 3}
        if state.task_type == "chat":
            args["chat_id"] = state.chat_id
            args["user_id"] = state.user_id
        return args
    if failed_step.tool_name == "generate_file":
        return _build_generate_file_args(state.prompt, state=state)
    return {}
