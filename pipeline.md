# Pipeline: dynamic exam sections

Status: **planned, not started**. This document records a desired architectural change
so it isn't lost - nothing described here has been implemented.

## The problem

The platform hardcodes exactly three sections, named A, B and C, each with a fixed,
built-in meaning:

- **Section A** - multiple choice. Fixed behaviour: radio-button options, one correct
  letter stored as the answer.
- **Section B** - short answer. Fixed behaviour: a single textarea.
- **Section C** - long writeup with "select N of these to be marked". Fixed behaviour:
  a textarea plus a selection checkbox, gated by `Exam.section_c_required`.

This isn't a config default that happens to be A/B/C - it's baked into the code as
those three specific letters, with that specific meaning, in every layer:

- **Data model** - `app/models_course.py`: `Question.section` is a 1-character column
  (`String(1)`) with a comment reading `# A multiple choice, B short, C long`.
  `Exam.section_c_required` is a field name, not a per-section concept.
- **Bulk-import JSON** - `app/templates/admin/json_guide.html` / the import parser in
  `app/routers/admin.py`: the accepted shape is exactly three top-level keys,
  `multiple_choice`, `short_answer`, `long_writeup` - not an open list of sections.
- **Admin exam-builder UI** - `app/templates/admin/exam.html:67`: the three sections and
  their display names are a literal Python tuple in the template:
  `[('A','Section A: multiple choice'),('B','Section B: short answer'),('C','Section C: long writeup')]`.
  The "Section C questions to choose" field is its own named input
  (`section_c_required`), not a property any section can have.
- **Sitting page rendering** - `app/static/js/sit.js`'s `render()`/`capture()`: branches
  explicitly on `question.section === "A" | "B" | "C"` to decide what to draw and how to
  read the answer back.
- **Backtrack control** (added 2026-09-14) - three settings,
  `allow_backtrack_section_a/b/c`, one per hardcoded letter.
- **Submission PDF** - `app/pdf.py`: three separate, hand-written blocks
  (`section_a`/`section_b`/`section_c`), each with its own rendering rules.

A real exam is not always three sections, and the three fixed behaviours don't cover
every question type a lecturer might want (e.g. matching, ordered steps, a short
answer that should also require picking N of M, multiple images per question). Every
one of the touch points above would need to change together for this to become
genuinely dynamic.

## Desired end state

- **Sections are an ordered, open list**, not a fixed set of letters. An exam can have
  one section or eight.
- **Each section is fully described by data**, not by which letter it happens to be:
  - a stable key (for referencing it - replaces the "A"/"B"/"C" column value)
  - a display name/label
  - a question type / interaction behaviour (multiple choice, short answer, long
    writeup, "select N of M for marking", and room to add more later without another
    hardcoded branch everywhere)
  - any type-specific settings a section's behaviour needs (e.g. "how many of this
    section's questions must be selected for marking" as a property *of the section*,
    not a one-off `Exam.section_c_required` column that only ever meant Section C)
  - its own backtrack-allowed setting (superseding today's three platform-wide env
    flags - this becomes something authored per exam, not fixed platform-wide)
- **Authored from two places, kept in sync**:
  1. **The bulk-import JSON** - extended from the current fixed
     `{multiple_choice, short_answer, long_writeup}` shape to something like an ordered
     `"sections": [...]` list, each entry declaring its own key/name/type/settings and
     that section's questions.
  2. **The admin settings page** - a section builder: add, remove, reorder sections,
     name them, choose their type/settings, independent of whether the exam was
     originally built by pasting JSON or by hand.
- **Backward compatible** with every exam already built the old way. Existing
  `Question.section` values of `"A"`/`"B"`/`"C"` and the existing JSON shape should
  keep working, effectively as one built-in default "section template" - not a
  breaking change for exams that already exist across every connected course database.

## Why this is a bigger job than it looks (read before starting)

This session spent an entire live exam sitting fighting exactly this class of problem:
`Question`, `Exam`, `Attempt` and friends live in **each lecturer's own database**
(`CourseBase` in `app/models_course.py`), not the platform's own database. There is no
central migration runner for those - see `docs/DEPLOYMENT.md` and
`app/tenant_db.py:validate_schema`, which only ever checks that expected *tables*
exist, never columns. Changing `Question.section` from a fixed single letter to an
open key, or adding new columns for section metadata, means:

1. Every already-provisioned course database needs the schema change applied
   **before** the new code is deployed, or every query touching `questions`/`exams`
   breaks platform-wide the instant it ships - not just for the course being changed.
2. `docs/DATABASE_SCHEMA.sql` (what a *new* lecturer runs when connecting a database)
   needs updating in lockstep, or new signups get a schema the new code can't use
   correctly either.
3. This has to be planned and executed deliberately, in a quiet window, the same way
   the 2026-09-14 schema-isolation fix was - not shipped speculatively.

## Rough shape of the work (not a commitment, just a starting point)

1. Design the section-definition schema first (JSON shape + admin UI fields) before
   touching the database - get the shape right on paper, since it's expensive to
   change once real exams depend on it.
2. Data model: replace `Question.section` (1-char) with a reference to a section
   definition; move `Exam.section_c_required` into a per-section setting on whichever
   section actually needs a "select N" rule.
3. Migration: write it, document the manual per-tenant-database steps (same shape as
   this session's `course_mbs6011` schema move), and update `DATABASE_SCHEMA.sql`.
4. Bulk-import parser + `json_guide.html`: accept the new open `sections` shape;
   keep parsing the old fixed shape as a compatibility path.
5. Admin exam-builder UI (`admin/exam.html`): replace the hardcoded
   `[('A',...), ('B',...), ('C',...)]` loop with one driven by the exam's actual
   section list; add the section builder (add/remove/reorder/configure).
6. Sitting page (`sit.js`): replace the `section === "A"/"B"/"C"` branches in
   `render()`/`capture()`/`canGoPrevious()` with logic driven by each section's
   declared type and settings, including its own backtrack flag.
7. Submission PDF (`pdf.py`): replace the three hand-written section blocks with one
   renderer parameterised by section type.
8. Retire the three platform-wide `ALLOW_BACKTRACK_SECTION_*` env flags once
   per-section backtrack is authored per exam instead - or keep them as a
   platform-wide fallback for sections that don't specify their own.
