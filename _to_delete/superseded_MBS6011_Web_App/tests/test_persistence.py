"""Checks the durability promise: data survives redeploys, schema moves only on migration.

A Railway redeploy runs `alembic upgrade head` and then starts the app. Nothing in that
path may drop, recreate or silently reshape a table that holds real submissions. These
tests simulate several deploys against one database and assert the data is still there.

    python3 tests/test_persistence.py
"""

import os
import subprocess
import sys
import tempfile

WORK_DIR = tempfile.mkdtemp(prefix="mbs6011_persist_")
DB_PATH = os.path.join(WORK_DIR, "persist.db")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

BASE_ENV = {
    **os.environ,
    "DATABASE_URL": f"sqlite:///{DB_PATH}",
    "SECRET_KEY": "persistence-test-secret-0123456789abcdefghij",
    "ADMIN_EMAIL": "lecturer@unza.zm",
    "ADMIN_PASSWORD": "first-admin-password",
    "MAIL_BACKEND": "console",
}
os.environ.update(BASE_ENV)
sys.path.insert(0, ROOT)

failures = []
checks = 0


def check(label, condition, detail=""):
    global checks
    checks += 1
    print(f"  {'PASS' if condition else 'FAIL'}  {label} {'' if condition else detail}")
    if not condition:
        failures.append(label)


def deploy(env_overrides=None):
    """One deploy: run migrations in a fresh process, exactly as the Procfile does."""
    env = dict(BASE_ENV)
    env.update(env_overrides or {})
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=120,
    )
    return result


def main():
    from sqlalchemy import create_engine, inspect, select, text
    from sqlalchemy.orm import sessionmaker

    print("\n1. The first deploy builds the schema")
    result = deploy()
    check("migration succeeded", result.returncode == 0, result.stderr[-400:])
    engine = create_engine(f"sqlite:///{DB_PATH}")
    tables = set(inspect(engine).get_table_names())
    for name in ("students", "exams", "questions", "attempts", "answers",
                 "incidents", "snapshots", "app_logs", "admins"):
        check(f"{name} table exists", name in tables, str(sorted(tables)))
    check("alembic stamped the revision", "alembic_version" in tables)

    print("\n2. Real data goes in")
    from app.models import Answer, Attempt, Exam, Question, Student
    from app.security import hash_password
    from datetime import datetime, timedelta

    Session = sessionmaker(bind=engine)
    with Session() as db:
        student = Student(full_name="Chanda Mulenga", email="chanda@unza.zm",
                          computer_number="2026123456",
                          password_hash=hash_password("a-good-long-password"),
                          is_verified=True, is_approved=True)
        exam = Exam(title="Test 1", duration_minutes=90, section_c_required=1, is_open=True)
        db.add_all([student, exam])
        db.flush()
        question = Question(exam_id=exam.id, section="B", order_index=1, prompt="Why?", marks=5)
        db.add(question)
        db.flush()
        started = datetime.utcnow()
        attempt = Attempt(exam_id=exam.id, student_id=student.id, started_at=started,
                          deadline_at=started + timedelta(minutes=90),
                          submitted_at=started, is_locked=True,
                          pdf_filename="paper.pdf", pdf_bytes=b"%PDF-1.4 pretend")
        db.add(attempt)
        db.flush()
        db.add(Answer(attempt_id=attempt.id, question_id=question.id,
                      value="Because margin funds acquisition.", selected=True))
        db.commit()
        student_id, attempt_id = student.id, attempt.id
    check("a submitted attempt is stored", attempt_id is not None)

    print("\n3. A redeploy runs migrations again and changes nothing")
    result = deploy()
    check("second migration is a no-op that succeeds", result.returncode == 0, result.stderr[-400:])
    with Session() as db:
        check("the student is still there",
              db.get(Student, student_id) is not None)
        stored = db.get(Attempt, attempt_id)
        check("the attempt is still there", stored is not None)
        check("the PDF bytes survived", stored.pdf_bytes == b"%PDF-1.4 pretend")
        check("the answer survived",
              db.scalar(select(Answer).where(Answer.attempt_id == attempt_id)).value.startswith("Because"))

    print("\n4. Starting the app does not touch the schema")
    from app.db import Base
    import app.main as main_module

    # Look for an actual call, in source only: the phrase appears in a docstring
    # explaining why it is not used, and stale .pyc files would match a plain grep.
    found = subprocess.run(
        ["grep", "-rn", "--include=*.py", r"create_all(", os.path.join(ROOT, "app")],
        capture_output=True, text=True,
    )
    check("the app never calls create_all", found.returncode != 0,
          "found: " + found.stdout.strip()[:300] + " - the app would reshape the database on boot")

    before = set(inspect(engine).get_table_names())
    main_module.check_schema()
    main_module.ensure_admin()
    after = set(inspect(engine).get_table_names())
    check("start-up added no tables", before == after, str(after - before))
    with Session() as db:
        check("start-up did not disturb the data", db.get(Attempt, attempt_id) is not None)

    print("\n5. The admin password follows the environment variable")
    from app.models import Admin
    from app.security import verify_password

    with Session() as db:
        admin = db.scalar(select(Admin))
        check("admin account created", admin is not None)
        check("first password works", verify_password("first-admin-password", admin.password_hash))

    os.environ["ADMIN_PASSWORD"] = "rotated-admin-password"
    from app.config import get_settings
    get_settings.cache_clear()
    import importlib
    import app.config as config_module
    importlib.reload(config_module)
    importlib.reload(main_module)
    main_module.ensure_admin()
    with Session() as db:
        admin = db.scalar(select(Admin))
        check("rotated password now works",
              verify_password("rotated-admin-password", admin.password_hash))
        check("the old password stops working",
              not verify_password("first-admin-password", admin.password_hash))
    with Session() as db:
        check("rotating the password kept the data", db.get(Attempt, attempt_id) is not None)

    print("\n6. Schema changes only happen when a migration is written")
    with engine.connect() as connection:
        revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar()
    check("a revision is stamped", bool(revision), str(revision))
    heads = subprocess.run([sys.executable, "-m", "alembic", "heads"],
                           cwd=ROOT, env=BASE_ENV, capture_output=True, text=True)
    check("the database is at head", revision in heads.stdout, heads.stdout.strip())
    current = subprocess.run([sys.executable, "-m", "alembic", "current"],
                             cwd=ROOT, env=BASE_ENV, capture_output=True, text=True)
    check("alembic agrees nothing is pending", revision in current.stdout, current.stdout.strip())

    # If the models and the migrations had drifted apart, autogenerate would want to emit
    # operations. An empty diff is the proof that the two are in step.
    check("models and migrations are in step", not _autogenerate_diff(engine, Base),
          "autogenerate wants changes - a migration is missing")

    print(f"\n{checks - len(failures)} of {checks} checks passed")
    if failures:
        print("FAILURES:\n  - " + "\n  - ".join(failures))
        return 1
    return 0


def _autogenerate_diff(engine, Base):
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    from app import models  # noqa: F401 - registers the tables

    with engine.connect() as connection:
        context = MigrationContext.configure(connection)
        diff = compare_metadata(context, Base.metadata)
    # Ignore the bookkeeping table alembic manages itself.
    return [entry for entry in diff if "alembic_version" not in str(entry)]


if __name__ == "__main__":
    code = main()
    print(f"\nDatabase left at {DB_PATH}")
    sys.exit(code)
