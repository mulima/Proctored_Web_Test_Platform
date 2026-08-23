# Deploying and running a sitting

A practical walkthrough. The README explains the design; this is what to actually do.

This now happens in two stages: someone deploys the **platform** once (Part 1), then
each lecturer **signs up** and connects their own database (Part 2) - that second
stage is what used to be "deploy a second instance for a second course." One running
platform now serves many courses.

---

## Part 1 — deploying the platform (once, by whoever operates it)

### 1. Push the code

```bash
cd Proctored_Web_Test_Platform
git init && git add . && git commit -m "Proctored web test platform"
gh repo create test-platform --private --source=. --push
```

### 2. Create the Railway project

1. railway.app → **New Project → Deploy from GitHub repo** → pick the repo.
2. The first build will fail or the app will start unhealthy. That is expected — there is
   no platform database yet.

### 3. Attach Postgres — this is the *platform* database

**New → Database → PostgreSQL**, in the same project. Railway injects `DATABASE_URL` into
the app service. This holds lecturer accounts and course settings, not any course's
own data — each lecturer connects their own database for that, separately, after
signing up.

> Check this before the platform is used for anything real. Without `DATABASE_URL` the
> app silently falls back to a SQLite file inside the container, and on Railway that
> file is destroyed on the next deploy — taking every lecturer account with it.

### 4. Set the variables

App service → **Variables**:

```
SECRET_KEY=<paste the output of the command below>
CREDENTIAL_ENCRYPTION_KEY=<paste the output of the second command below>
BASE_URL=https://<your-app>.up.railway.app
MAIL_BACKEND=smtp
MAIL_FROM=Proctored Test Platform <no-reply@yourdomain.zm>
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USERNAME=<a gmail address>
SMTP_PASSWORD=<a 16-character Google app password, not your login password>
APP_NAME=Proctored Web Test Platform
```

These are platform-wide - shared by every course. Course-specific fields (code, title,
institution, database) are no longer env vars; each lecturer sets those themselves,
per course, at `/<slug>/admin/setup` after signing up.

Generate the two keys:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

`BASE_URL` must match the deployed domain exactly. Every verification and alert link,
for every course, is built from it — a wrong value sends every student on every course
to a dead address.

### 5. Redeploy and check

Watch the deploy log for:

```
INFO  [alembic.runtime.migration] Running upgrade  -> <revision>, platform schema: lecturers and platform_logs
```

Then open `https://<your-app>.up.railway.app/healthz` — it should return `{"ok": true}`.

---

## Part 2 — a lecturer setting up a course

This is what replaces "deploy a second instance." No Railway access is needed for this
part - it all happens through the app.

### 1. Sign up

`https://<your-app>.up.railway.app/signup` — email, password, and a course address
(`slug`): students will reach the course at `/<slug>/register`, `/<slug>/login`, etc.

### 2. Confirm your email

A link is sent immediately - nothing else, including signing in, works until you
follow it. Lost it? `/resend-lecturer`. Then sign in at `/<slug>/admin/login`.

### 3. Provision a database

A free [Supabase](https://supabase.com) project works well. Run the SQL in
[`docs/DATABASE_SCHEMA.md`](DATABASE_SCHEMA.md) against it once - paste
[`docs/DATABASE_SCHEMA.sql`](DATABASE_SCHEMA.sql) directly into Supabase's SQL editor,
or run the equivalent Alembic migration yourself if you'd rather. Two things matter
about which connection string you copy:

- Use the **direct** or **session-pooler** string, not a transaction-mode pooler
  (Supabase's port-6543 one) - DDL and some driver behaviour aren't reliable through
  transaction pooling.
- If the platform is deployed somewhere without IPv6 egress (Railway, notably) and your
  database host resolves IPv6-only, use the session-pooler / IPv4-reachable string
  instead of the direct one, or the platform's connection attempt will time out with
  "Network is unreachable."

### 3. Connect it

Paste the connection string at `/<slug>/admin/setup`. The platform connects, checks
every expected table exists, and only then activates the course - if a table's
missing, it says which one and nothing is saved. Nothing else (registration, sign-in,
sitting) works until this succeeds.

### 4. Set branding and build the paper

Same page: course code, title and institution - they appear on every page, email and
PDF for this course from here on.

Then `/<slug>/admin`: create an exam, set the duration and how many Section C
questions to choose, add questions or paste a `quiz_data.json` into **Bulk import**.
Check the paper. **Then** click Open — that releases it to every verified student on
*this course* at once and closes any other open exam for it.

There is no password reset flow for a lecturer account, by design, matching the
original admin model. If you're locked out, that needs a direct database fix on the
platform database (`lecturers` table) - see `app/security.py`'s `hash_password`.

---

## Optional: switch on phone detection

Without a model the platform runs fine, but the phone detector is off and says so. To
enable it you need two files, both too large for Git (they are in `.gitignore`):

**The model.** Any Ultralytics ONNX export with a phone class works. If you built the
desktop version, its model files are the obvious source:

```bash
cp phone_detector_oiv7.onnx Proctored_Web_Test_Platform/app/static/models/phone_detector.onnx
```

**The runtime.** Download `ort.min.js` and its `.wasm` files from the `onnxruntime-web`
distribution and put them in `app/static/js/`.

Then either commit them (removing the `.gitignore` lines) or, better, host them as Railway
volume contents or on a CDN you control and point `MBS_DATA.modelUrl` and `ortUrl` at
them in `app/templates/sit.html`. This is a platform-wide setting - one model file serves
every course on the deployment.

Bear the size in mind: the Open Images model is roughly 14 MB and every student downloads
it once, at the start of the sitting. Thirty students on one campus link is 400 MB in a
burst. Consider the smaller COCO model, or accept that phone detection is off and rely on
the full-screen rules and physical invigilation.

---

## Before each sitting (per course)

- [ ] The course's database is connected and shows as ready at `/<slug>/admin/setup`.
- [ ] `BASE_URL` (platform-wide) matches the live domain.
- [ ] Send one test email — register a throwaway account on your course and confirm the
      link arrives. A `MAIL_BACKEND` that silently fails is the single most likely thing
      to go wrong.
- [ ] Build the paper in `/<slug>/admin` and read it through. Check the Section C count.
- [ ] Sit the paper yourself end to end on a machine like the students'. Confirm the PDF
      arrives in your inbox with the incident appendix.
- [ ] Delete your own test attempt and account from the roster (`/<slug>/admin/students`).
- [ ] Tell students to register **at least a day before**, not on the morning, at
      `/<slug>/register`.
- [ ] Point students at `/privacy` and get their acknowledgement.
- [ ] Tell them to close Zoom, Teams and anything else using the camera — a camera already
      held by another application will fail to start and be recorded as unavailable.
- [ ] Open the exam only when you are ready. Opening releases it to everyone on your
      course at once - it has no effect on any other course on the platform.

---

## During the sitting

The admin panel (`/<slug>/admin`) updates live:

- **Overview** — attempts in progress, flagged count.
- **Flagged** — papers that reached the strike threshold. Each one shows the incident log
  and the evidence snapshots side by side.
- **Logs** — every event on this course, searchable. `INCIDENT_`, `LOGIN_FAILED` and
  `*_EMAIL_FAILED` are the ones worth watching.

Alert emails arrive as flags are raised, rate-limited to one per candidate every few
minutes so one restless student cannot bury the inbox.

**If a student loses their connection:** their answers are held in the page and resync
when it returns; the page shows an "Offline" chip and tells them not to close the window.
The server clock keeps running — a network outage does not extend the paper. If they lose
the machine entirely, their saved answers are in the database up to the last successful
save, and you can see exactly where that was in the log.

**If a student's camera fails:** the test still runs. A `CAMERA_UNAVAILABLE` incident is
recorded so the failure is on the record at the time rather than argued about later.

---

## After the sitting

1. Close the exam in the admin panel.
2. Every submission is already in your inbox as a PDF with the incident appendix.
3. **Submissions** lists every attempt; **Flagged** narrows it to the ones to review.
4. Read a flagged log next to your own invigilation notes before acting on it. A flag is a
   prompt to look, not a finding — and where the server's second opinion disagrees with
   the browser's, trust neither and look at the image yourself.

---

## Costs and limits, honestly

Railway's starter plan runs this comfortably for a cohort of this size per course: the
server does no inference during a sitting, only bookkeeping. Storage is the thing that
grows — submission PDFs and evidence snapshots live in each course's own database. A
40-student sitting with a handful of snapshots each is a few tens of megabytes.

Every course's database is a separate connection pool this one process holds open once
it's been used. That's unremarkable for a handful of courses; if the platform ever hosts
a large number of simultaneously-active courses, connection-pool pressure across all of
them at once is the thing to watch (`app/tenant_db.py` has no eviction yet).

The container sleeps when idle on some plans. The first request after a sleep is slow,
which matters if a student is the first to arrive. Hit the site yourself a few minutes
before the sitting starts.
