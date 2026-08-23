# Course database schema

This is what **your own** database needs before you can connect it at `/<your-course>/admin/setup`.
The platform never creates or alters your tables - only checks that they're
there. If a table's missing, setup tells you which one and stops; nothing is
saved or activated until every table exists.

## Set it up

You need a Postgres database somewhere you control - a free
[Supabase](https://supabase.com) project works well. Whichever you use, two
things matter:

- Use the **direct** or **session-pooler** connection string, not a
  transaction-mode pooler (e.g. Supabase's port-6543 pooler) - DDL and some
  driver features don't behave reliably through transaction pooling.
- If your host resolves to IPv6-only and you're deploying this platform on
  infrastructure without IPv6 egress (Railway, notably), use the
  session-pooler / IPv4-reachable connection string instead of the direct one.

**Option A - paste the SQL.** Open your database's SQL editor (Supabase:
Project → SQL Editor) and run [`DATABASE_SCHEMA.sql`](DATABASE_SCHEMA.sql) in
this same folder, unmodified, once.

**Option B - run the migration yourself.** If you're comfortable with a Python
dev environment: edit `alembic_course.ini` in the repo root so
`sqlalchemy.url` points at your database, then run:

```bash
alembic -c alembic_course.ini upgrade head
```

Both produce the identical schema below - Option B just goes through Alembic
so you get proper migration tracking (an `alembic_version` table) if you ever
want to run future schema updates the same way. The platform's own
`validate_schema` check (run every time you connect a database) only looks
for the tables below - it doesn't care which option you used, or whether
`alembic_version` exists.

## Then

Copy your database's connection string and paste it at
`/<your-course>/admin/setup`. It's encrypted before being stored, and only
ever decrypted in-memory to open a connection - never shown back to you or
logged in the clear.

## The tables

Generated directly from `app/models_course.py` via `alembic_course`, so it can
never silently drift from what the running app actually expects:

| Table | Holds |
|---|---|
| `students` | Registered accounts: name, email, computer number, password, verification/approval state |
| `exams` | One row per exam you create; `is_open` controls which one (if any) students can sit |
| `questions` | Section A/B/C questions belonging to an exam |
| `attempts` | One row per student per exam sitting: timing, lock state, strike count, the stored submission PDF |
| `answers` | One row per question a student has answered or selected |
| `incidents` | The proctoring event log for each attempt |
| `snapshots` | Evidence images captured when a flag is raised |
| `app_logs` | This course's durable application log |

Full `CREATE TABLE` statements: [`DATABASE_SCHEMA.sql`](DATABASE_SCHEMA.sql).
