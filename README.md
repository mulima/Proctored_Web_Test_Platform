# Proctored Web Test Platform

A proctored online test platform for university coursework tests and examinations.

Students pre-register with their name, email and computer number, confirm the address
through an emailed link, and sit the paper in the browser under full-screen enforcement
and webcam proctoring. Submissions produce a locked PDF that is emailed to the
administrator with the proctoring log attached.

**Nothing in the code names a particular course.** One deployment serves one course at a
time, and which course that is comes entirely from environment variables — `COURSE_CODE`,
`COURSE_TITLE` and `INSTITUTION`. Leave them unset and the platform describes itself
generically; set them and the course appears on every page, email and PDF. To run a second
course, deploy a second instance with different variables.

This is the web successor to a Windows executable built for the same purpose. The
proctoring rules, the warn-then-flag policy and the PDF layout are carried over; the
delivery, storage and administration are new.

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

## Deploying to Railway

### 1. Create the service

Push this folder to a Git repository and create a Railway project from it. Nixpacks
detects Python and installs `requirements.txt`.

### 2. Add Postgres

In the Railway project, **New → Database → PostgreSQL**. Railway injects `DATABASE_URL`
into the app service automatically. Without it the app falls back to a local SQLite file,
which on Railway means **your data disappears on every deploy** — so check the variable is
present before a real sitting.

### 3. Set the variables

| Variable | Required | Notes |
|---|---|---|
| `APP_NAME` | no | Shown when no course is configured (default *Proctored Web Test Platform*) |
| `COURSE_CODE` | no | e.g. `MBS6011` — appears in headers, subjects and filenames |
| `COURSE_TITLE` | no | e.g. `MBS6011: E-Business Strategies and Models` |
| `INSTITUTION` | no | Shown in the footer and on the submission PDF |
| `ADMIN_EMAIL` | yes | Admin login, and where submissions and alerts are sent |
| `ADMIN_PASSWORD` | yes | Read at every app start; see below |
| `SECRET_KEY` | yes | `python -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `BASE_URL` | yes | e.g. `https://mbs6011.up.railway.app` — used in verification links |
| `MAIL_BACKEND` | yes | `smtp`, `resend`, or `console` |
| `MAIL_FROM` | yes | e.g. `MBS6011 <no-reply@yourdomain.zm>` |
| `DATABASE_URL` | auto | Injected by the Postgres addon |
| `REQUIRE_ADMIN_APPROVAL` | no | `true` turns the roster into an allowlist you approve |
| `ALLOWED_EMAIL_DOMAINS` | no | e.g. `unza.zm` to restrict who may register |
| `STRIKE_FLAG_AFTER` | no | Incidents before a paper is flagged (default 3) |

`.env.example` lists the rest, including every proctoring threshold.

**`SECRET_KEY` must be set explicitly.** If it is missing the app generates one at boot,
which changes on every restart — signing out every student mid-sitting and invalidating
every unopened verification link.

### 4. Deploy

`railway.json` runs migrations then starts the server:

```
alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

Health check is `/healthz`, which touches the database so a broken connection surfaces as
a failed deploy rather than a working page over a dead pool.

### 5. Sign in and build the paper

0. Set `COURSE_CODE`, `COURSE_TITLE` and `INSTITUTION` for the course this deployment
   serves, if you have not already.
1. Go to `/admin/login`, sign in with `ADMIN_EMAIL` and `ADMIN_PASSWORD`.
2. Create an exam, set the duration and how many Section C questions to choose.
3. Add questions, or paste a `quiz_data.json` into **Bulk import**.
4. Check the paper. **Then** click Open — that releases it to every verified student at
   once and closes any other open exam.

---

## Email

Three backends, chosen with `MAIL_BACKEND`:

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
full, so failures are logged (`SUBMISSION_EMAIL_FAILED` in the admin log) and the PDF is
still stored in the database and downloadable from the admin panel.

---

## The database

Alembic owns the schema; the app never calls `create_all`. Concretely:

- a redeploy runs `alembic upgrade head`, which is a no-op when nothing has changed;
- data survives redeploys, restarts and password rotations;
- the schema changes **only** when someone writes a migration and it is applied.

To change the schema:

```bash
alembic revision --autogenerate -m "what changed"   # review the generated file
alembic upgrade head
```

`tests/test_persistence.py` asserts all of this against a real database, including that
the models and migrations have not drifted apart.

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
python3 tests/test_end_to_end.py    # registration to submission, 95 checks
python3 tests/test_browser.py       # real Chromium, fake camera, 54 checks
python3 tests/test_persistence.py   # migrations and durability, 29 checks
```

`test_browser.py` needs Playwright's Chromium. It injects `tests/fake_camera.js`, which
replaces `getUserMedia` with a canvas stream whose pixels the test controls, so the
presence rules are driven deliberately rather than by hoping a synthetic pattern trips
them.

All three force `MAIL_BACKEND=console` and run against a throwaway database. Keep it that
way if you add tests — nothing here should reach a real inbox or a real sitting.

---

## Layout

```
app/
  main.py              start-up, admin bootstrap, routing, health check
  config.py            every environment variable
  db.py                engine and session
  models.py            schema
  security.py          argon2 hashing, signed cookies, single-use tokens
  mailer.py            console / smtp / resend
  proctor.py           incident rules, strike model, clock ratchet
  pdf.py               submission PDF with the incident appendix
  vision.py            server-side second opinion on a snapshot
  logging_service.py   the durable application log
  routers/             auth, exam, admin
  templates/           Jinja2
  static/js/proctor.js the two-stage detection cascade
  static/js/sit.js     sitting page: rendering, blackout, autosave, submission
alembic/               migrations
tests/                 three suites, 178 checks
docs/DEPLOYMENT.md     step-by-step Railway walkthrough and pre-sitting checklist
```
