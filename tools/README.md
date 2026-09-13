# tools/

Local, standalone helper scripts. Nothing here is imported by `app/` or deployed to
Railway - these run on your own machine, when you're preparing an exam, not as part
of the live service.

## paper_to_json.py

Converts a scanned or photographed exam paper into the JSON shape the admin panel's
"Bulk import" box accepts (`multiple_choice` / `short_answer` / `long_writeup`), using
a local Ollama vision model. The paper's content never leaves your machine.

**Setup (one time):**

```bash
pip install -r tools/requirements.txt
ollama pull qwen2.5vl:7b
```

Use a real *local* model tag - not one suffixed `:cloud`. Those run on Ollama's own
servers, which sends your exam content off your machine and defeats the point. If
`qwen2.5vl:7b` is too slow on your hardware, `qwen2.5vl:3b` is faster and noticeably
less accurate; there's no GPU-free way to get both speed and accuracy for OCR-heavy
work like this.

**Run:**

```bash
python tools/paper_to_json.py page1.jpg page2.jpg page3.jpg -o draft.json
# or, for a PDF:
python tools/paper_to_json.py exam_scan.pdf -o draft.json
```

It prints a per-section count and a list of things worth checking by eye (a question
with fewer than two options, marks that read as 0, any `[illegible]` marker) to
`draft.json`. **Read the file before using it** - this is a draft, not a finished
import. The admin panel's Bulk import box commits straight to the database the moment
you submit it, with no review step of its own, so this script's warning list is the
only checkpoint before that happens.

Once you're satisfied with `draft.json`, open the relevant exam in the admin panel,
paste its contents into "Bulk import," and submit as normal.

If a paper is long enough that one request struggles, split it into two runs by page
range and merge the two output files by hand - the script doesn't do that for you.
