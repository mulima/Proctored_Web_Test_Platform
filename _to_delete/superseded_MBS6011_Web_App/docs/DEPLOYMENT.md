# Deploying and running a sitting

A practical walkthrough. The README explains the design; this is what to actually do.

---

## Part 1 — first deployment (once)

### 1. Push the code

```bash
cd MBS6011_Web_App
git init && git add . && git commit -m "MBS6011 test platform"
gh repo create mbs6011-test-platform --private --source=. --push
```

### 2. Create the Railway project

1. railway.app → **New Project → Deploy from GitHub repo** → pick the repo.
2. The first build will fail or the app will start unhealthy. That is expected — there is
   no database and no admin password yet.

### 3. Attach Postgres

**New → Database → PostgreSQL**, in the same project. Railway injects `DATABASE_URL` into
the app service.

> Check this before every real sitting. Without `DATABASE_URL` the app silently falls back
> to a SQLite file inside the container, and on Railway that file is destroyed on the next
> deploy — taking every submission with it.

### 4. Set the variables

App service → **Variables**:

```
ADMIN_EMAIL=mchibuye@gmail.com
ADMIN_PASSWORD=<a long password you choose>
SECRET_KEY=<paste the output of the command below>
BASE_URL=https://<your-app>.up.railway.app
MAIL_BACKEND=smtp
MAIL_FROM=MBS6011 Test Platform <no-reply@yourdomain.zm>
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USERNAME=<your gmail address>
SMTP_PASSWORD=<a 16-character Google app password, not your login password>
COURSE_CODE=MBS6011
COURSE_TITLE=MBS6011: E-Business Strategies and Models
INSTITUTION=University of Zambia - Graduate School of Business
```

Generate the secret key:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

`BASE_URL` must match the deployed domain exactly. Verification links are built from it,
and a wrong value sends every student to a dead address.

### 5. Redeploy and check

Watch the deploy log for:

```
INFO  [alembic.runtime.migration] Running upgrade  -> <revision>, initial schema
[startup] Admin account created for <your email>.
```

Then open `https://<your-app>.up.railway.app/healthz` — it should return `{"ok": true}`.

### 6. Sign in

`/admin/login`, with `ADMIN_EMAIL` and `ADMIN_PASSWORD`.

There is no password reset flow, by design. To change the admin password, change
`ADMIN_PASSWORD` and redeploy; the new value takes effect at start-up.

---

## Part 2 — optional: switch on phone detection

Without a model the platform runs fine, but the phone detector is off and says so. To
enable it you need two files, both too large for Git (they are in `.gitignore`):

**The model.** Copy one from the desktop app folder
(`E-Business/MBS6011_Test_App/`, or the CSC6711 folder if you never copied it across):

```bash
cp phone_detector_oiv7.onnx MBS6011_Web_App/app/static/models/phone_detector.onnx
```

**The runtime.** Download `ort.min.js` and its `.wasm` files from the `onnxruntime-web`
distribution and put them in `app/static/js/`.

Then either commit them (removing the `.gitignore` lines) or, better, host them as Railway
volume contents or on a CDN you control and point `MBS_DATA.modelUrl` and `ortUrl` at
them in `app/templates/sit.html`.

Bear the size in mind: the Open Images model is roughly 14 MB and every student downloads
it once, at the start of the sitting. Thirty students on one campus link is 400 MB in a
burst. Consider the smaller COCO model, or accept that phone detection is off and rely on
the full-screen rules and physical invigilation.

---

## Part 3 — before each sitting

- [ ] `DATABASE_URL` is present and points at Postgres.
- [ ] `BASE_URL` matches the live domain.
- [ ] Send one test email — register a throwaway account and confirm the link arrives.
      A `MAIL_BACKEND` that silently fails is the single most likely thing to go wrong.
- [ ] Build the paper in the admin panel and read it through. Check the Section C count.
- [ ] Sit the paper yourself end to end on a machine like the students'. Confirm the PDF
      arrives in the admin inbox with the incident appendix.
- [ ] Delete your own test attempt and account from the roster.
- [ ] Tell students to register **at least a day before**, not on the morning.
- [ ] Point students at `/privacy` and get their acknowledgement.
- [ ] Tell them to close Zoom, Teams and anything else using the camera — a camera already
      held by another application will fail to start and be recorded as unavailable.
- [ ] Open the exam only when you are ready. Opening releases it to everyone at once.

---

## Part 4 — during the sitting

The admin panel updates live:

- **Overview** — attempts in progress, flagged count.
- **Flagged** — papers that reached the strike threshold. Each one shows the incident log
  and the evidence snapshots side by side.
- **Logs** — every event, searchable. `INCIDENT_`, `LOGIN_FAILED` and `*_EMAIL_FAILED` are
  the ones worth watching.

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

## Part 5 — after the sitting

1. Close the exam in the admin panel.
2. Every submission is already in your inbox as a PDF with the incident appendix.
3. **Submissions** lists every attempt; **Flagged** narrows it to the ones to review.
4. Read a flagged log next to your own invigilation notes before acting on it. A flag is a
   prompt to look, not a finding — and where the server's second opinion disagrees with
   the browser's, trust neither and look at the image yourself.

---

## Costs and limits, honestly

Railway's starter plan runs this comfortably for a cohort of this size: the server does no
inference during the sitting, only bookkeeping. Storage is the thing that grows —
submission PDFs and evidence snapshots live in Postgres. A 40-student sitting with a
handful of snapshots each is a few tens of megabytes.

The container sleeps when idle on some plans. The first request after a sleep is slow,
which matters if a student is the first to arrive. Hit the site yourself a few minutes
before the sitting starts.
