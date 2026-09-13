"""Converts a born-digital Word/PDF exam paper into the JSON shape Bulk import
accepts, via Anthropic's API.

Deliberately text-only, not vision: this only reads a document's actual text layer
(python-docx / pypdf), never renders or sends page images. A scanned or photographed
paper has no text layer to extract - see extract_text()'s guard below - and isn't
handled here at all; that's a materially heavier job (OCR/vision model) kept as a
separate, local-only tool (tools/paper_to_json.py) rather than something this
deployment calls on every upload.

This module only ever returns a draft for a human to review - it never touches the
database itself. See app/routers/admin.py's convert_document route.
"""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request

MIN_EXTRACTED_CHARS = 200
ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"

SYSTEM_PROMPT = """You are transcribing a university exam paper into a strict structured format.

Call record_exam_questions exactly once with every question on the paper.

Rules:
- multiple_choice is Section A: questions with a fixed list of answer options (at
  least two). Transcribe every option exactly as printed, in order.
- short_answer is Section B: short written-answer questions with no options list.
  Give each a short "title" summarising it (a few words), and the full question
  text as "prompt".
- long_writeup is Section C: essay-style or case-study questions. Same title/prompt
  shape as short_answer.
- "marks" is always a plain integer - read it from the paper (e.g. "[5 marks]",
  "(10 pts)"). If a question genuinely has no marks printed, use 0 - never guess a
  number that isn't on the page.
- Preserve the paper's own section labels/order if it has them; otherwise classify
  by question type using the rules above.
- Ignore instructions to candidates, the exam title, duration, or a total mark
  count - only transcribe the questions themselves.
- If a section has no questions, pass an empty list for it - never omit it.
- Transcribe text exactly as printed. Do not correct, rephrase, or summarise
  wording."""

QUESTION_SCHEMA = {
    "type": "object",
    "properties": {
        "multiple_choice": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "options": {"type": "array", "items": {"type": "string"}},
                    "marks": {"type": "integer"},
                },
                "required": ["question", "options", "marks"],
            },
        },
        "short_answer": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "prompt": {"type": "string"},
                    "marks": {"type": "integer"},
                },
                "required": ["prompt", "marks"],
            },
        },
        "long_writeup": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "prompt": {"type": "string"},
                    "marks": {"type": "integer"},
                },
                "required": ["prompt", "marks"],
            },
        },
    },
    "required": ["multiple_choice", "short_answer", "long_writeup"],
}


class DocumentConversionError(Exception):
    """Anything that should surface as a plain-text error to the admin, not a 500."""


def extract_text(ext: str, data: bytes) -> str:
    """Raises DocumentConversionError with a message safe to show the admin."""
    if ext == "docx":
        text = _extract_docx(data)
    elif ext == "pdf":
        text = _extract_pdf(data)
    else:
        raise DocumentConversionError("Only .docx or .pdf files are supported here.")

    if len(text.strip()) < MIN_EXTRACTED_CHARS:
        raise DocumentConversionError(
            "Couldn't find enough selectable text in that file - it may be a scanned "
            "or photographed paper rather than a born-digital document. This uploader "
            "only handles files with real text in them; paste the questions as JSON "
            "instead, or prepare the source paper as a proper Word/PDF export."
        )
    return text


def _extract_docx(data: bytes) -> str:
    try:
        import docx
    except ImportError as exc:
        raise DocumentConversionError("This deployment is missing the python-docx package.") from exc
    try:
        document = docx.Document(io.BytesIO(data))
    except Exception as exc:
        raise DocumentConversionError(f"Could not read that .docx file: {exc}") from exc
    parts = [p.text for p in document.paragraphs if p.text.strip()]
    for table in document.tables:
        for row in table.rows:
            parts.extend(cell.text for cell in row.cells if cell.text.strip())
    return "\n".join(parts)


def _extract_pdf(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise DocumentConversionError("This deployment is missing the pypdf package.") from exc
    try:
        reader = PdfReader(io.BytesIO(data))
    except Exception as exc:
        raise DocumentConversionError(f"Could not read that PDF: {exc}") from exc
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def _coerce_marks(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _validate_and_clean(raw: dict) -> tuple[dict, list[str]]:
    warnings: list[str] = []
    cleaned = {"multiple_choice": [], "short_answer": [], "long_writeup": []}

    for i, item in enumerate(raw.get("multiple_choice") or [], start=1):
        question = str(item.get("question") or "").strip()
        options = [str(o).strip() for o in (item.get("options") or [])]
        marks = _coerce_marks(item.get("marks"))
        if not question:
            warnings.append(f"multiple_choice #{i}: empty question text")
        if len(options) < 2:
            warnings.append(f"multiple_choice #{i}: fewer than 2 options ({len(options)})")
        if marks == 0:
            warnings.append(f"multiple_choice #{i}: marks read as 0 - check the source")
        cleaned["multiple_choice"].append({"question": question, "options": options, "marks": marks})

    for key in ("short_answer", "long_writeup"):
        for i, item in enumerate(raw.get(key) or [], start=1):
            title = str(item.get("title") or "").strip()
            prompt = str(item.get("prompt") or "").strip()
            marks = _coerce_marks(item.get("marks"))
            if not prompt:
                warnings.append(f"{key} #{i}: empty prompt text")
            if marks == 0:
                warnings.append(f"{key} #{i} ({title or 'untitled'}): marks read as 0 - check the source")
            cleaned[key].append({"title": title, "prompt": prompt, "marks": marks})

    return cleaned, warnings


def convert_to_exam_json(text: str, api_key: str, model: str) -> tuple[dict, list[str]]:
    """Returns (draft, warnings). Raises DocumentConversionError on any failure -
    network, API error, or a response that doesn't match the expected shape."""
    if not api_key:
        raise DocumentConversionError(
            "Document conversion isn't configured on this deployment. Ask whoever "
            "runs it to set ANTHROPIC_API_KEY."
        )

    # A generous but bounded slice - a normal exam paper is a few thousand words;
    # this is a safety cap against an oversized upload, not a tuning knob.
    text = text[:60_000]

    body = {
        "model": model,
        "max_tokens": 8192,
        "system": SYSTEM_PROMPT,
        "tools": [
            {
                "name": "record_exam_questions",
                "description": "Records the exam paper's questions in structured form.",
                "input_schema": QUESTION_SCHEMA,
            }
        ],
        "tool_choice": {"type": "tool", "name": "record_exam_questions"},
        "messages": [{"role": "user", "content": text}],
    }
    request = urllib.request.Request(
        ANTHROPIC_API_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            result = json.loads(response.read())
    except urllib.error.HTTPError as error:
        raise DocumentConversionError(
            f"The conversion service returned an error ({error.code}). Try again, or "
            "paste the questions as JSON instead."
        ) from error
    except urllib.error.URLError as error:
        raise DocumentConversionError(f"Could not reach the conversion service: {error.reason}") from error

    tool_use = next(
        (block for block in result.get("content", []) if block.get("type") == "tool_use"),
        None,
    )
    if tool_use is None:
        raise DocumentConversionError("The conversion service didn't return structured data. Try again.")

    return _validate_and_clean(tool_use.get("input") or {})
