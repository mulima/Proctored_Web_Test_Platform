# Proctored Web Test Platform

A proctored online test platform for university coursework tests and examinations -
now a shared platform rather than a one-course-per-deployment app. Any lecturer can
sign up, name their own course, and connect their own database; students pre-register
with their name, email and computer number, confirm the address through an emailed
link, and sit the paper in the browser under full-screen enforcement and webcam
proctoring. Submissions produce a locked PDF that is emailed to the lecturer with the
proctoring log attached.

**One running instance serves many courses.** Each lecturer gets their own address -
`/<course-slug>/...` - their own branding (course code, title, institution, set through
the app, not env vars), and their own database, which they provision themselves. The
platform itself never sees or creates a course's students, exams or attempts; it only
knows which lecturer owns which slug and how to reach the database they've pointed at.
See [`docs/DATABASE_SCHEMA.md`](docs/DATABASE_SCHEMA.md) for exactly what a lecturer's
own database needs before they can connect it.

This is the web successor to a Windows executable built for the same purpose. The
proctoring rules, the warn-then-flag policy and the PDF layout are carried over; the
delivery, storage and administration are new - and, as of this rearchitecture, the
platform itself is multi-tenant.

---

## What runs where, and why

**Detection runs in the student's browser, not on the server.**

Continuous proctoring means several frames a second, per candidate. Thirty candidates is
thirty video streams; no small container keeps up with that, and it would put continuous
webcam video on the wire. So the browser does the continuous work with the same two-stage
cascade the desktop build used:

| Stage | What it does | Cost |
|---|---|---|
| 1. Prefilter | 80×60 grayscale frame, motion plus a lit-rectangle test | ~0.1 ms |
| 2. Detector | ONNX phone model via `onnxruntime-web`, only on frames stage 1 wakes | ~50–200 ms |

On a still scene stage 1 skips most inference entirely. Frames never leave the machine.

**The server does exactly one inference per incident.** When a flag is raised, that single
frame is uploaded as evidence and re-checked server-side, giving the marker a second
opinion: the browser said phone, and here is whether the server agreed. That is one model
run per incident instead of four per second per candidate.

A skipped frame is *not* a negative reading. Treating it as one would keep resetting the
confirmation window and a phone held still would never be confirmed — the same bug that
had to be fixed in the desktop version.

---

## Two databases

**The platform database** (`DATABASE_URL`) - deployed once, by whoever runs this
service. Holds only `lecturers` (accounts, course branding, an encrypted pointer to
each lecturer's own database) and `platform_logs` (signup/login/database-connection
events). Alembic owns this schema; `alembic upgrade head` targets it exactly like a
single-tenant deploy always has.

**Each course's own database** - provisioned and owned by the lecturer, connected at
`/<slug>/admin/setup`. Holds `students`, `exams`, `questions`, `attempts`, `answers`,
`incidents`, `snapshots`, `app_logs` - the schema documented in
[`docs/DATABASE_SCHEMA.md`](docs/DATABASE_SCHEMA.md). The platform **never creates or
alters these tables** - it only connects and validates that they already exist,
exactly the way a lecturer is expected to have set them up. A connection string is
rejected outright, with the specific table(s) named, if anything's missing.

`app/tenant_db.py` creates and caches one engine per lecturer, lazily, the first time
that course's database is actually needed by a request.

---

## Deploying the platform

### 1. Create the service

Push this repository to a Git repository and create a Railway project from it.
Nixpacks detects Python and installs `requirements.txt`.

### 2. Add Postgres - this is the *platform* database

In the Railway project, **New → Database → PostgreSQL**. Railway injects `DATABASE_URL`
into the app service automatically. This holds lecturer accounts, not any course's data
— each lecturer brings their own database for that, separately, through the app.

### 3. Set the variables

| Variable | Required | Notes |
|---|---|---|
| `APP_NAME` | no | Shown on platform-level pages with no course context (default *Proctored Web Test Platform*) |
| `SECRET_KEY` | yes | `python -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `CREDENTIAL_ENCRYPTION_KEY` | yes | `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` — encrypts every lecturer's stored database connection string. Deliberately separate from `SECRET_KEY`; rotating this key alone would strand every stored connection string, so treat it as precious. |
| `BASE_URL` | yes | e.g. `https://yourplatform.up.railway.app` — used in every verification/alert link, for every course |
| `MAIL_BACKEND` | yes | `smtp`, `resend`, or `console` — shared across every course on this deployment |
| `MAIL_FROM` | yes | e.g. `Proctored Test Platform <no-reply@yourdomain.zm>` |
| `ALERT_EMAIL` | no | Operator address for critical alerts; blank keeps alerts in application logs |
| `ALERT_THRESHOLD` | no | Repeated login/access events required before an alert (default 5) |
| `ALERT_WINDOW_MINUTES` | no | Time window for repeated login/access detection (default 10) |
| `DATABASE_URL` | auto | Injected by the Postgres addon — the *platform* database |
| `REQUIRE_ADMIN_APPROVAL` | no | Default a lecturer's course starts with; `true` turns their roster into an allowlist they approve by hand |
| `ALLOWED_EMAIL_DOMAINS` | no | e.g. `unza.zm` to restrict who may register, across every course |
| `STRIKE_FLAG_AFTER` | no | Incidents before a paper is flagged (default 3) |

`.env.example` lists the rest, including every proctoring threshold.

**`SECRET_KEY` and `CREDENTIAL_ENCRYPTION_KEY` must both be set explicitly.** If
`SECRET_KEY` is missing the app generates one at boot, which changes on every restart —
signing out every signed-in user mid-session. `CREDENTIAL_ENCRYPTION_KEY` has no such
fallback: without it, no lecturer can connect a database at all.

### 4. Deploy

`railway.json` runs migrations then starts the server:

```
alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

This migrates the *platform* schema only (`lecturers`, `platform_logs`) - never a
lecturer's own database; see `alembic_course/` below for that.

Health check is `/healthz`, which touches the platform database so a broken connection
surfaces as a failed deploy rather than a working page over a dead pool.

---

## For lecturers: setting up a course

1. Go to `/signup`, create an account, and choose a course address (`slug`) - students
   will reach you at `/<slug>/register`, `/<slug>/login`, etc.
2. Confirm your email - a link is sent immediately, and nothing else (signing in
   included) works until you follow it. Lost it? `/resend-lecturer`.
3. Sign in at `/<slug>/admin/login`, land on `/<slug>/admin/setup`. Provision a database - a free
   [Supabase](https://supabase.com) project works well - with the tables in
   [`docs/DATABASE_SCHEMA.md`](docs/DATABASE_SCHEMA.md), then paste its connection
   string in. Nothing else works until this succeeds.
4. Set your course code, title and institution on the same page - they appear on every
   page, email and PDF from here on. Optionally set your own email delivery there too;
   leave it blank to use the platform's default.
5. Go to `/<slug>/admin`, create an exam, set the duration and how many Section C
   questions to choose.
6. Add questions, or paste a `quiz_data.json` into **Bulk import**.
7. Check the paper. **Then** click Open — that releases it to every verified student at
   once and closes any other open exam for your course.

---

## Email

Three backends, chosen with `MAIL_BACKEND`. This is the **platform default** - what a
course uses if its own lecturer never sets anything at `/<slug>/admin/setup`'s email
section. A lecturer can override backend, from-address, SMTP or Resend credentials for
just their own course there; secrets are encrypted the same way the database
connection string is, and never shown back once saved. Leaving it all unset (the
common case) falls back to whatever's below:

- **`console`** — prints messages to the log. The default, so the app runs with nothing
  configured. Verification links appear in the Railway log, which is fine for a trial and
  useless for a real sitting.
- **`smtp`** — any SMTP server. A Gmail app password works (`smtp.gmail.com`, port 587);
  so does a university relay. Watch the daily send limit if a whole cohort registers at
  once.
- **`resend`** — the Resend HTTP API. Useful because it needs no outbound SMTP port, which
  some hosts block. Set `RESEND_API_KEY`; verify your sending domain or mail will land in
  spam.

Sending never raises into a request. A paper must not fail to submit because an inbox is
full, so failures are logged (`SUBMISSION_EMAIL_FAILED` in that course's admin log) and
the PDF is still stored in the lecturer's database and downloadable from their admin panel.

---

## The database(s)

Alembic owns both schemas; the app never calls `create_all` anywhere. Concretely:

- a redeploy runs `alembic upgrade head` against the **platform** database, which is a
  no-op when nothing has changed;
- a lecturer's own database is never migrated by this app automatically - see
  `alembic_course/` and [`docs/DATABASE_SCHEMA.md`](docs/DATABASE_SCHEMA.md);
- data survives redeploys, restarts and password rotations, on both sides;
- the platform schema changes **only** when someone writes a migration in `alembic/`
  and it is applied.

To change the platform schema:

```bash
alembic revision --autogenerate -m "what changed"   # review the generated file
alembic upgrade head
```

To change the course schema (rare - it's the contract every lecturer's own database is
validated against, so treat it as a breaking change to communicate, not a routine edit):

```bash
alembic -c alembic_course.ini revision --autogenerate -m "what changed"
# regenerate docs/DATABASE_SCHEMA.sql:
alembic -c alembic_course.ini upgrade head --sql > docs/DATABASE_SCHEMA.sql
# (then strip the alembic_version bookkeeping lines - see the file's own history for the shape)
```

`tests/test_persistence.py` asserts the platform side of this against a real database,
including that the platform models and migrations have not drifted apart.

---

## Optional: the server-side second opinion

Drop a phone-detection model into `app/static/models/`:

- `phone_detector_oiv7.onnx` — Open Images V7, class 339 *Mobile phone* (preferred)
- `phone_detector_yolo11n.onnx` or `phone_detector.onnx` — COCO, class 67 *cell phone*

Any Ultralytics ONNX export with a phone class will do; the files the desktop app used are the obvious source. Copy one to
`app/static/models/phone_detector.onnx` so the browser can fetch it, and the browser-side
detector switches on too. Without a model the platform still runs: presence and
full-screen rules work, snapshots are stored with a verdict of `not_checked`, and both the
admin panel and the candidate's status chip say so rather than implying detection is
running.

`onnxruntime-web` must also be present at `app/static/js/ort.min.js` for browser-side
detection. Both are gitignored because of their size — see `docs/DEPLOYMENT.md`.

---

## What it honestly catches

**Reliable.** Leaving full screen, minimising, switching away. Screen-capture keys and the
common shortcuts. Copy, paste and right-click. An empty chair. A second face. Reloading or
reconnecting. Every one is timestamped, stored, and printed into the submitted PDF.

**Good but not certain.** A phone held up to the screen. The detector fires on a
device-shaped object in the camera's field of view. It misses a phone held low or off to
the side, and it occasionally fires on a dark notebook or a wallet. Treat every phone flag
as a prompt to look at the candidate, never as proof.

**Not caught at all.** A second person off camera. A phone below desk level. Notes on
paper. Another device beside the laptop. A different machine entirely. No browser-based
proctor catches these.

**Structurally weaker than the desktop app.** A browser cannot see other windows or
running processes, so the desktop build's screen-capture and remote-access process watch
has no equivalent here. It also cannot force kiosk mode — the full-screen blackout is the
enforcement, and it works by making leaving pointless rather than impossible.

Physical invigilation still does the real work. This narrows what an invigilator has to
watch and gives you a written record afterwards.

**Tell students in advance** what the camera does, that no video is recorded or
transmitted, that one still image is captured and sent when a flag is raised, and that a
flag is reviewed by a person. `/privacy` says all of this; point them at it before the
sitting. Consent given in advance avoids most of the arguments afterwards.

---

## Tests

```bash
python3 tests/test_end_to_end.py    # registration to submission
python3 tests/test_browser.py       # real Chromium, fake camera
python3 tests/test_persistence.py   # migrations and durability
```

**These three suites predate the multi-tenant rearchitecture and are not yet updated for
it** - they exercise the old single-tenant, un-prefixed routes (`/register` rather than
`/<slug>/register`) and the old single-database model. Treat them as due a rewrite rather
than a working safety net until that happens. A manual end-to-end smoke test covering
signup → database setup → registration → verification → sitting → submission → admin
review, plus a cross-tenant isolation check, was run by hand during this rearchitecture;
it is not yet captured as an automated suite.

`test_browser.py` needs Playwright's Chromium. It injects `tests/fake_camera.js`, which
replaces `getUserMedia` with a canvas stream whose pixels the test controls, so the
presence rules are driven deliberately rather than by hoping a synthetic pattern trips
them.

All three force `MAIL_BACKEND=console` and run against a throwaway database. Keep it that
way if you update them — nothing here should reach a real inbox or a real sitting.

---

## Layout

```
app/
  main.py                 platform routing: /, /signup, healthz, privacy, and the
                           slug-prefixed course router wrapping auth/exam/admin
  config.py                platform-level environment variables only
  db.py                    engine and session for the PLATFORM database
  tenant_db.py             one engine per lecturer's own database, cached lazily
  tenant_crypto.py         encrypts/decrypts a lecturer's stored connection string
  models_platform.py       Lecturer, PlatformLog - the platform schema
  models_course.py         Student, Exam, ... AppLog - the course schema
  security.py               argon2 hashing, slug-scoped signed cookies, single-use tokens
  mailer.py                console / smtp / resend
  proctor.py                incident rules, strike model, clock ratchet
  pdf.py                    submission PDF with the incident appendix
  vision.py                 server-side second opinion on a snapshot
  logging_service.py        record() for a course's own log, record_platform() for the platform's
  routers/                  auth, exam, admin - all mounted under /{slug}
  templates/                Jinja2; course_url()/course() globals do the slug-prefixing
  static/js/proctor.js      the two-stage detection cascade
  static/js/sit.js          sitting page: rendering, blackout, autosave, submission
alembic/                    platform schema migrations
alembic_course/             course schema migrations - never run against this app's own deploy
docs/DATABASE_SCHEMA.md     what a lecturer's own database needs, and how to set it up
docs/DATABASE_SCHEMA.sql    the same schema as plain CREATE TABLE statements
tests/                      three suites - see the Tests section above
docs/DEPLOYMENT.md          step-by-step Railway walkthrough and pre-sitting checklist
```
