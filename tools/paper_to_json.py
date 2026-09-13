"""Turn a scanned/photographed exam paper into a draft for the app's Bulk Import box,
using a local Ollama vision model. Runs entirely on your own machine - page images and
their text never leave it, and nothing here talks to the deployed app or any cloud API.

This is a STANDALONE tool, deliberately outside app/. The deployed server (Railway)
cannot reach a model running on your laptop, and installing an LLM runtime on Railway
isn't practical for this kind of hobby deployment - so the workflow is: run this here,
review the draft it prints, then paste the reviewed JSON into
"{course}/admin/exams/{id}" -> Bulk import, same as pasting any other JSON there.

This produces a DRAFT, not a finished import. Read every question it extracts before
pasting it in - check marks, section (A/B/C), and that no text was misread from the
scan. The importer commits straight to the database the moment you submit that box;
it has no preview step of its own, so this script's printed summary is the only
checkpoint before that happens.

Setup (one-time):
    pip install -r tools/requirements.txt
    ollama pull qwen2.5vl:7b        # or a smaller/larger tag - see --model below
    ollama serve                    # usually already running as a background service

Usage:
    python tools/paper_to_json.py page1.jpg page2.jpg page3.jpg -o draft.json
    python tools/paper_to_json.py exam_scan.pdf -o draft.json
    python tools/paper_to_json.py exam_scan.pdf --model qwen2.5vl:3b   # faster, less accurate

If a paper is long enough that one request struggles (very slow, or the model starts
dropping questions), split it into two runs by page range and merge the two JSON files
by hand - this script does not attempt that merge for you.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path

import requests

SCHEMA_INSTRUCTIONS = """You are transcribing a scanned exam paper into a strict JSON format.

Output ONLY a single JSON object with exactly these three top-level keys - no prose,
no markdown fences, no commentary before or after it:

{
  "multiple_choice": [
    {"question": "...", "options": ["...", "...", "..."], "marks": 2}
  ],
  "short_answer": [
    {"title": "...", "prompt": "...", "marks": 5}
  ],
  "long_writeup": [
    {"title": "...", "prompt": "...", "marks": 20}
  ]
}

Rules:
- multiple_choice is Section A: questions with a fixed list of answer options (at least
  two). Transcribe every option exactly as printed, in order.
- short_answer is Section B: short written-answer questions with no options list.
  Give each a short "title" summarising it (a few words), and the full question text
  as "prompt".
- long_writeup is Section C: essay-style or case-study questions. Same title/prompt
  shape as short_answer.
- "marks" is always a plain integer - read it from the paper (e.g. "[5 marks]",
  "(10 pts)"). If a question genuinely has no marks printed, use 0 - never guess a
  number that isn't on the page.
- Preserve the paper's own section labels/order if it has them; otherwise classify by
  question type using the rules above.
- If the paper includes instructions to candidates, exam title, duration, or a total
  mark count, IGNORE them - only transcribe the questions themselves into the schema.
- If a section has no questions, use an empty list for it - never omit a key.
- Transcribe text exactly as printed. Do not correct, rephrase, or summarise wording.
  If a word is genuinely illegible, write [illegible] in its place rather than guessing.
"""

DEFAULT_MODEL = "qwen2.5vl:7b"
DEFAULT_HOST = "http://localhost:11434"


def load_page_images(paths: list[str]) -> list[bytes]:
    """Returns one PNG per page, in the order given. A single .pdf argument is
    rasterised page by page; image files are read as-is."""
    if len(paths) == 1 and paths[0].lower().endswith(".pdf"):
        return rasterise_pdf(paths[0])
    images = []
    for p in paths:
        data = Path(p).read_bytes()
        images.append(data)
    return images


def rasterise_pdf(pdf_path: str) -> list[bytes]:
    try:
        import fitz  # PyMuPDF
    except ImportError:
        print(
            "PDF input needs PyMuPDF: pip install -r tools/requirements.txt",
            file=sys.stderr,
        )
        raise SystemExit(1)
    doc = fitz.open(pdf_path)
    images = []
    # 220 DPI is a reasonable floor for a vision model to read printed exam text
    # reliably without producing enormous, slow-to-process images.
    zoom = 220 / 72
    matrix = fitz.Matrix(zoom, zoom)
    for page in doc:
        pix = page.get_pixmap(matrix=matrix)
        images.append(pix.tobytes("png"))
    doc.close()
    return images


def call_ollama(images: list[bytes], model: str, host: str) -> str:
    encoded = [base64.b64encode(img).decode("ascii") for img in images]
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": SCHEMA_INSTRUCTIONS},
            {
                "role": "user",
                "content": "Transcribe this exam paper's questions into the JSON format described above.",
                "images": encoded,
            },
        ],
        "format": "json",
        "stream": False,
        "options": {"temperature": 0},
    }
    try:
        resp = requests.post(f"{host}/api/chat", json=body, timeout=1800)
    except requests.exceptions.ConnectionError:
        print(
            f"Could not reach Ollama at {host}. Is it running? Try: ollama serve",
            file=sys.stderr,
        )
        raise SystemExit(1)
    if resp.status_code == 404:
        print(
            f"Ollama doesn't have model {model!r} pulled yet. Run:\n"
            f"    ollama pull {model}",
            file=sys.stderr,
        )
        raise SystemExit(1)
    resp.raise_for_status()
    return resp.json()["message"]["content"]


def coerce_marks(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def validate_and_clean(raw: dict) -> tuple[dict, list[str]]:
    """Returns a cleaned draft plus a list of human-readable warnings - things worth
    checking by eye before this goes anywhere near the Bulk import box."""
    warnings: list[str] = []
    cleaned = {"multiple_choice": [], "short_answer": [], "long_writeup": []}

    for i, item in enumerate(raw.get("multiple_choice") or [], start=1):
        if not isinstance(item, dict):
            warnings.append(f"multiple_choice #{i}: not an object, skipped")
            continue
        question = str(item.get("question") or "").strip()
        options = [str(o).strip() for o in (item.get("options") or [])]
        marks = coerce_marks(item.get("marks"))
        if not question:
            warnings.append(f"multiple_choice #{i}: empty question text")
        if len(options) < 2:
            warnings.append(f"multiple_choice #{i}: fewer than 2 options ({len(options)})")
        if marks == 0:
            warnings.append(f"multiple_choice #{i}: marks read as 0 - check the scan")
        cleaned["multiple_choice"].append({"question": question, "options": options, "marks": marks})

    for key in ("short_answer", "long_writeup"):
        for i, item in enumerate(raw.get(key) or [], start=1):
            if not isinstance(item, dict):
                warnings.append(f"{key} #{i}: not an object, skipped")
                continue
            title = str(item.get("title") or "").strip()
            prompt = str(item.get("prompt") or "").strip()
            marks = coerce_marks(item.get("marks"))
            if not prompt:
                warnings.append(f"{key} #{i}: empty prompt text")
            if marks == 0:
                warnings.append(f"{key} #{i} ({title or 'untitled'}): marks read as 0 - check the scan")
            cleaned[key].append({"title": title, "prompt": prompt, "marks": marks})

    for section, items in cleaned.items():
        for i, item in enumerate(items, start=1):
            text = item.get("prompt") or item.get("question") or ""
            if "[illegible]" in text:
                warnings.append(f"{section} #{i}: contains [illegible] - the model couldn't read part of the scan")

    return cleaned, warnings


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("files", nargs="+", help="Page image files (in order), or a single PDF")
    parser.add_argument("-o", "--output", default="draft.json", help="Where to write the draft JSON (default: draft.json)")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"Ollama vision model tag (default: {DEFAULT_MODEL})")
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"Ollama API host (default: {DEFAULT_HOST})")
    args = parser.parse_args()

    print(f"Reading {len(args.files)} input file(s)...", file=sys.stderr)
    images = load_page_images(args.files)
    print(f"Sending {len(images)} page image(s) to {args.model} at {args.host} - this can take a while on CPU...", file=sys.stderr)

    content = call_ollama(images, args.model, args.host)
    try:
        raw = json.loads(content)
    except json.JSONDecodeError:
        print("Model did not return valid JSON. Raw output follows:\n", file=sys.stderr)
        print(content, file=sys.stderr)
        raise SystemExit(1)

    cleaned, warnings = validate_and_clean(raw)
    counts = {k: len(v) for k, v in cleaned.items()}

    Path(args.output).write_text(json.dumps(cleaned, indent=2), encoding="utf-8")

    print(f"\nWrote {args.output}", file=sys.stderr)
    print(f"  multiple_choice: {counts['multiple_choice']}", file=sys.stderr)
    print(f"  short_answer:    {counts['short_answer']}", file=sys.stderr)
    print(f"  long_writeup:    {counts['long_writeup']}", file=sys.stderr)

    if warnings:
        print(f"\n{len(warnings)} thing(s) worth checking by eye before importing:", file=sys.stderr)
        for w in warnings:
            print(f"  - {w}", file=sys.stderr)
    else:
        print("\nNo automatic warnings - still read it before importing.", file=sys.stderr)

    print(
        f"\nNext step: open {args.output}, read every question, fix anything wrong, "
        "then paste the final JSON into that exam's Bulk import box in the admin panel.",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
