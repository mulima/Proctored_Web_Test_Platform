from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import csv
from datetime import datetime, timedelta
from io import BytesIO
from io import StringIO
from pathlib import Path
import sys
from zipfile import ZipFile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from fastapi.testclient import TestClient
from PyPDF2 import PdfReader
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app import pdf
from app.config import settings
from app.db import Base
from app.models_course import (
    Answer,
    Attempt,
    CourseBase,
    Exam,
    Question,
    Student,
    SubmissionAuditEvent,
)
from app.models_platform import Lecturer
from app.security import hash_password, make_session_cookie
from app.tenant_crypto import encrypt


@pytest.fixture()
def app_env(tmp_path, monkeypatch):
    # Keep alerts and outbound mail inert during tests.
    settings.mail_backend = "console"
    settings.alert_email = ""
    settings.base_url = "http://testserver"
    settings.secret_key = "test-secret-key-123"
    settings.credential_encryption_key = Fernet.generate_key().decode()

    import app.tenant_crypto as tenant_crypto

    tenant_crypto._fernet.cache_clear()

    platform_db_path = tmp_path / "platform.sqlite3"
    platform_engine = create_engine(
        f"sqlite:///{platform_db_path}",
        connect_args={"check_same_thread": False},
        future=True,
    )
    PlatformSessionLocal = sessionmaker(
        bind=platform_engine, autoflush=False, expire_on_commit=False, future=True
    )
    Base.metadata.create_all(bind=platform_engine)

    # Patch runtime DB handles used by dependencies and health checks.
    import app.db as app_db
    import app.main as app_main
    import app.tenant_db as tenant_db

    monkeypatch.setattr(app_db, "engine", platform_engine)
    monkeypatch.setattr(app_db, "SessionLocal", PlatformSessionLocal)
    monkeypatch.setattr(app_main, "engine", platform_engine)
    monkeypatch.setattr(app_main, "SessionLocal", PlatformSessionLocal)

    tenant_db._engines.clear()
    tenant_db._sessionmakers.clear()

    yield {
        "platform_engine": platform_engine,
        "PlatformSessionLocal": PlatformSessionLocal,
        "tmp_path": tmp_path,
    }

    tenant_db._engines.clear()
    tenant_db._sessionmakers.clear()
    platform_engine.dispose()


def _make_course_db(path) -> sessionmaker:
    engine = create_engine(
        f"sqlite:///{path}",
        connect_args={"check_same_thread": False},
        future=True,
    )
    CourseBase.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def _create_lecturer(platform_session: Session, *, slug: str, course_db_url: str) -> Lecturer:
    lecturer = Lecturer(
        email=f"{slug}@example.com",
        password_hash=hash_password("lecturer-password"),
        slug=slug,
        is_verified=True,
        database_ready=True,
        course_storage_mode="external",
        database_url_encrypted=encrypt(course_db_url),
    )
    platform_session.add(lecturer)
    platform_session.commit()
    platform_session.refresh(lecturer)
    return lecturer


def _seed_exam_with_attempts(
    course_session_factory: sessionmaker,
    *,
    student_count: int,
    submitted: bool,
    started_delta_minutes: int = 10,
    duration_minutes: int = 90,
    show_submission_pdf: bool = True,
):
    with course_session_factory() as db:
        exam = Exam(
            title="Operations Test",
            duration_minutes=duration_minutes,
            total_marks=100,
            section_c_required=1,
            is_open=True,
            show_submission_pdf=show_submission_pdf,
        )
        db.add(exam)
        db.flush()

        q1 = Question(exam_id=exam.id, section="A", order_index=1, prompt="A?", options=["X", "Y"])
        q2 = Question(exam_id=exam.id, section="B", order_index=1, prompt="B?")
        q3 = Question(exam_id=exam.id, section="C", order_index=1, title="C1", prompt="C?")
        db.add_all([q1, q2, q3])
        db.flush()

        students = []
        attempts = []
        started_at = datetime.utcnow() - timedelta(minutes=started_delta_minutes)
        deadline_at = started_at + timedelta(minutes=duration_minutes)
        for i in range(student_count):
            student = Student(
                full_name=f"Student {i+1}",
                email=f"student{i+1}@example.com",
                computer_number=f"C{i+1:03d}",
                password_hash=hash_password("student-pass"),
                is_verified=True,
                is_approved=True,
            )
            db.add(student)
            db.flush()
            attempt = Attempt(
                exam_id=exam.id,
                student_id=student.id,
                started_at=started_at,
                deadline_at=deadline_at,
                is_locked=False,
            )
            db.add(attempt)
            db.flush()
            db.add_all(
                [
                    Answer(attempt_id=attempt.id, question_id=q1.id, value="A", selected=False),
                    Answer(attempt_id=attempt.id, question_id=q2.id, value="Short answer", selected=False),
                    Answer(attempt_id=attempt.id, question_id=q3.id, value=f"Long answer {i+1}", selected=True),
                ]
            )
            students.append(student)
            attempts.append(attempt)

        db.commit()

        if submitted:
            for attempt in attempts:
                attempt = db.get(Attempt, attempt.id)
                attempt.is_locked = True
                attempt.submitted_at = datetime.utcnow()
                attempt.submission_mode = "manual"
                db.refresh(attempt, attribute_names=["student", "exam", "answers", "incidents"])
                answers_map = {a.question_id: a for a in attempt.answers}
                attempt.pdf_bytes = pdf.build(attempt, answers_map, lecturer=lecturer_stub(exam.title))
                attempt.pdf_filename = f"seed_{attempt.id}.pdf"
            db.commit()

        return exam.id, [s.id for s in students], [a.id for a in attempts]


def lecturer_stub(title_prefix: str):
    class _Lecturer:
        file_prefix = "exam"
        footer = "Test Footer"
        subtitle = "Test Subtitle"

    return _Lecturer()


def _set_student_cookie(client: TestClient, *, slug: str, student_id: int):
    token = make_session_cookie({"role": "student", "slug": slug, "id": student_id})
    client.cookies.set(settings.session_cookie, token, path=f"/{slug}")


def _set_admin_cookie(client: TestClient, *, slug: str, lecturer_id: int):
    token = make_session_cookie({"role": "admin", "slug": slug, "id": lecturer_id})
    client.cookies.set(settings.session_cookie, token, path=f"/{slug}")


def test_two_students_submitting_concurrently(app_env):
    course_path = app_env["tmp_path"] / "course_one.sqlite3"
    course_url = f"sqlite:///{course_path}"
    course_session_factory = _make_course_db(course_path)

    with app_env["PlatformSessionLocal"]() as platform_db:
        lecturer = _create_lecturer(platform_db, slug="course-a", course_db_url=course_url)

    _, student_ids, attempt_ids = _seed_exam_with_attempts(course_session_factory, student_count=2, submitted=False)

    c1 = TestClient(__import__("app.main", fromlist=["app"]).app, follow_redirects=False)
    c2 = TestClient(__import__("app.main", fromlist=["app"]).app, follow_redirects=False)
    _set_student_cookie(c1, slug="course-a", student_id=student_ids[0])
    _set_student_cookie(c2, slug="course-a", student_id=student_ids[1])

    def submit(client):
        return client.post("/course-a/api/submit", json={"auto": False, "reason": ""})

    with ThreadPoolExecutor(max_workers=2) as pool:
        r1 = pool.submit(submit, c1).result()
        r2 = pool.submit(submit, c2).result()

    assert r1.status_code == 200
    assert r2.status_code == 200

    with course_session_factory() as db:
        for attempt_id in attempt_ids:
            attempt = db.get(Attempt, attempt_id)
            assert attempt.is_locked is True
            assert attempt.pdf_bytes is not None


def test_two_courses_isolated_data_paths(app_env):
    course_a_path = app_env["tmp_path"] / "course_a.sqlite3"
    course_b_path = app_env["tmp_path"] / "course_b.sqlite3"
    course_a_url = f"sqlite:///{course_a_path}"
    course_b_url = f"sqlite:///{course_b_path}"
    a_session = _make_course_db(course_a_path)
    b_session = _make_course_db(course_b_path)

    with app_env["PlatformSessionLocal"]() as platform_db:
        _create_lecturer(platform_db, slug="alpha", course_db_url=course_a_url)
        _create_lecturer(platform_db, slug="beta", course_db_url=course_b_url)

    _, a_students, _ = _seed_exam_with_attempts(a_session, student_count=1, submitted=True)
    _, b_students, _ = _seed_exam_with_attempts(b_session, student_count=1, submitted=True)

    app = __import__("app.main", fromlist=["app"]).app
    client_a = TestClient(app, follow_redirects=False)
    client_b = TestClient(app, follow_redirects=False)
    _set_student_cookie(client_a, slug="alpha", student_id=a_students[0])
    _set_student_cookie(client_b, slug="beta", student_id=b_students[0])

    ra = client_a.get("/alpha/my-submission.pdf")
    rb = client_b.get("/beta/my-submission.pdf")
    assert ra.status_code == 200
    assert rb.status_code == 200
    assert ra.content != rb.content


def test_dashboard_pdf_ownership(app_env):
    course_path = app_env["tmp_path"] / "ownership.sqlite3"
    course_url = f"sqlite:///{course_path}"
    course_session_factory = _make_course_db(course_path)
    with app_env["PlatformSessionLocal"]() as platform_db:
        _create_lecturer(platform_db, slug="owner", course_db_url=course_url)

    _, student_ids, _ = _seed_exam_with_attempts(course_session_factory, student_count=2, submitted=True)

    app = __import__("app.main", fromlist=["app"]).app
    c1 = TestClient(app, follow_redirects=False)
    c2 = TestClient(app, follow_redirects=False)
    _set_student_cookie(c1, slug="owner", student_id=student_ids[0])
    _set_student_cookie(c2, slug="owner", student_id=student_ids[1])

    p1 = c1.get("/owner/my-submission.pdf")
    p2 = c2.get("/owner/my-submission.pdf")
    assert p1.status_code == 200
    assert p2.status_code == 200

    t1 = "\n".join(page.extract_text() or "" for page in PdfReader(BytesIO(p1.content)).pages)
    t2 = "\n".join(page.extract_text() or "" for page in PdfReader(BytesIO(p2.content)).pages)
    assert "Student 1" in t1
    assert "Student 2" in t2
    assert "Student 2" not in t1
    assert "Student 1" not in t2


def test_dashboard_identity_is_scoped_to_student_session_and_not_cacheable(app_env):
    course_path = app_env["tmp_path"] / "dashboard_identity.sqlite3"
    course_url = f"sqlite:///{course_path}"
    course_session_factory = _make_course_db(course_path)
    with app_env["PlatformSessionLocal"]() as platform_db:
        _create_lecturer(platform_db, slug="dash", course_db_url=course_url)

    _, student_ids, _ = _seed_exam_with_attempts(course_session_factory, student_count=2, submitted=False)

    app = __import__("app.main", fromlist=["app"]).app
    c1 = TestClient(app, follow_redirects=False)
    c2 = TestClient(app, follow_redirects=False)
    _set_student_cookie(c1, slug="dash", student_id=student_ids[0])
    _set_student_cookie(c2, slug="dash", student_id=student_ids[1])

    r1 = c1.get("/dash/")
    r2 = c2.get("/dash/")

    assert r1.status_code == 200
    assert r2.status_code == 200
    assert "Hello, Student 1" in r1.text
    assert "Hello, Student 2" not in r1.text
    assert "Hello, Student 2" in r2.text
    assert "Hello, Student 1" not in r2.text
    assert r1.headers["cache-control"] == "no-store, private"
    assert r2.headers["vary"] == "Cookie"


def test_student_login_redirects_to_own_dashboard(app_env):
    course_path = app_env["tmp_path"] / "student_login.sqlite3"
    course_url = f"sqlite:///{course_path}"
    course_session_factory = _make_course_db(course_path)
    with app_env["PlatformSessionLocal"]() as platform_db:
        _create_lecturer(platform_db, slug="logincheck", course_db_url=course_url)

    _, student_ids, _ = _seed_exam_with_attempts(course_session_factory, student_count=1, submitted=False)
    with course_session_factory() as db:
        student = db.get(Student, student_ids[0])

    app = __import__("app.main", fromlist=["app"]).app
    client = TestClient(app, follow_redirects=False)
    response = client.post(
        "/logincheck/login",
        data={"email": student.email, "password": "student-pass"},
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/logincheck/"
    assert settings.session_cookie in client.cookies

    dashboard = client.get("/logincheck/")
    assert dashboard.status_code == 200
    assert f"Hello, {student.full_name}" in dashboard.text
    assert student.computer_number in dashboard.text


def test_pdf_visibility_toggle(app_env):
    course_path = app_env["tmp_path"] / "visibility.sqlite3"
    course_url = f"sqlite:///{course_path}"
    course_session_factory = _make_course_db(course_path)
    with app_env["PlatformSessionLocal"]() as platform_db:
        _create_lecturer(platform_db, slug="visible", course_db_url=course_url)

    exam_id, student_ids, _ = _seed_exam_with_attempts(
        course_session_factory, student_count=1, submitted=True, show_submission_pdf=False
    )

    app = __import__("app.main", fromlist=["app"]).app
    client = TestClient(app, follow_redirects=False)
    _set_student_cookie(client, slug="visible", student_id=student_ids[0])
    hidden = client.get("/visible/my-submission.pdf")
    assert hidden.status_code == 404

    with course_session_factory() as db:
        exam = db.get(Exam, exam_id)
        exam.show_submission_pdf = True
        db.commit()

    shown = client.get("/visible/my-submission.pdf")
    assert shown.status_code == 200


def test_regeneration_changes_pdf_from_current_rows(app_env):
    course_path = app_env["tmp_path"] / "regen.sqlite3"
    course_url = f"sqlite:///{course_path}"
    course_session_factory = _make_course_db(course_path)
    with app_env["PlatformSessionLocal"]() as platform_db:
        lecturer = _create_lecturer(platform_db, slug="regen", course_db_url=course_url)

    exam_id, _, attempt_ids = _seed_exam_with_attempts(course_session_factory, student_count=1, submitted=True)

    with course_session_factory() as db:
        attempt = db.get(Attempt, attempt_ids[0])
        before = attempt.pdf_bytes
        # Simulate corrected answer content before regeneration.
        c_answer = db.scalar(
            select(Answer).where(Answer.attempt_id == attempt.id).order_by(Answer.question_id.desc())
        )
        c_answer.value = "Updated long answer for regenerated PDF"
        db.commit()

    app = __import__("app.main", fromlist=["app"]).app
    admin_client = TestClient(app, follow_redirects=False)
    _set_admin_cookie(admin_client, slug="regen", lecturer_id=lecturer.id)
    response = admin_client.post(f"/regen/admin/exams/{exam_id}/regenerate-pdfs")
    assert response.status_code == 303

    with course_session_factory() as db:
        attempt = db.get(Attempt, attempt_ids[0])
        assert attempt.pdf_bytes is not None
        assert attempt.pdf_bytes != before


def test_deadline_submission_auto_converts_and_locks(app_env):
    course_path = app_env["tmp_path"] / "deadline.sqlite3"
    course_url = f"sqlite:///{course_path}"
    course_session_factory = _make_course_db(course_path)
    with app_env["PlatformSessionLocal"]() as platform_db:
        _create_lecturer(platform_db, slug="deadline", course_db_url=course_url)

    _, student_ids, attempt_ids = _seed_exam_with_attempts(
        course_session_factory,
        student_count=1,
        submitted=False,
        started_delta_minutes=200,
        duration_minutes=30,
    )

    app = __import__("app.main", fromlist=["app"]).app
    client = TestClient(app, follow_redirects=False)
    _set_student_cookie(client, slug="deadline", student_id=student_ids[0])
    response = client.post("/deadline/api/submit", json={"auto": False, "reason": ""})
    assert response.status_code == 200

    with course_session_factory() as db:
        attempt = db.get(Attempt, attempt_ids[0])
        assert attempt.is_locked is True
        assert attempt.submission_mode == "automatic"
        assert attempt.auto_submit_reason == "Time expired."
        assert attempt.pdf_bytes is not None


def test_failed_pdf_generation_rolls_back_lock_state(app_env, monkeypatch):
    course_path = app_env["tmp_path"] / "pdf_fail.sqlite3"
    course_url = f"sqlite:///{course_path}"
    course_session_factory = _make_course_db(course_path)
    with app_env["PlatformSessionLocal"]() as platform_db:
        _create_lecturer(platform_db, slug="failpdf", course_db_url=course_url)

    _, student_ids, attempt_ids = _seed_exam_with_attempts(course_session_factory, student_count=1, submitted=False)

    import app.routers.exam as exam_router

    def explode(*args, **kwargs):
        raise RuntimeError("simulated PDF build failure")

    monkeypatch.setattr(exam_router.pdf, "build", explode)

    app = __import__("app.main", fromlist=["app"]).app
    client = TestClient(app, follow_redirects=False, raise_server_exceptions=False)
    _set_student_cookie(client, slug="failpdf", student_id=student_ids[0])

    response = client.post("/failpdf/api/submit", json={"auto": False, "reason": ""})
    assert response.status_code == 500

    with course_session_factory() as db:
        attempt = db.get(Attempt, attempt_ids[0])
        assert attempt.is_locked is False
        assert attempt.submitted_at is None
        assert attempt.pdf_bytes is None


def test_attempt_review_workflow_compare_regenerate_and_history(app_env):
    course_path = app_env["tmp_path"] / "review_workflow.sqlite3"
    course_url = f"sqlite:///{course_path}"
    course_session_factory = _make_course_db(course_path)

    with app_env["PlatformSessionLocal"]() as platform_db:
        lecturer = _create_lecturer(platform_db, slug="reviewflow", course_db_url=course_url)

    _, _, attempt_ids = _seed_exam_with_attempts(course_session_factory, student_count=1, submitted=True)
    attempt_id = attempt_ids[0]

    # Mutate an answer after submit so compare detects mismatch.
    with course_session_factory() as db:
        answer = db.scalar(select(Answer).where(Answer.attempt_id == attempt_id).order_by(Answer.id.desc()))
        answer.value = "Edited after submission"
        db.commit()

    app = __import__("app.main", fromlist=["app"]).app
    admin_client = TestClient(app, follow_redirects=False)
    _set_admin_cookie(admin_client, slug="reviewflow", lecturer_id=lecturer.id)

    compare = admin_client.post(f"/reviewflow/admin/attempts/{attempt_id}/compare-pdf")
    assert compare.status_code == 303
    assert compare.headers["location"].endswith("?cmp=mismatch")

    regenerate = admin_client.post(f"/reviewflow/admin/attempts/{attempt_id}/regenerate-pdf")
    assert regenerate.status_code == 303
    assert regenerate.headers["location"].endswith("?regen=1")

    with course_session_factory() as db:
        attempt = db.get(Attempt, attempt_id)
        assert attempt.last_pdf_audit_match is False
        assert attempt.last_pdf_audit_stored_sha256
        assert attempt.last_pdf_audit_current_sha256
        history = db.scalars(
            select(SubmissionAuditEvent)
            .where(SubmissionAuditEvent.attempt_id == attempt_id)
            .order_by(SubmissionAuditEvent.id)
        ).all()
        assert len(history) >= 2
        assert history[-2].action == "compare"
        assert history[-2].status == "warning"
        assert history[-1].action == "regenerate"


def test_attempt_mark_reviewed_and_download_audit_report(app_env):
    course_path = app_env["tmp_path"] / "review_report.sqlite3"
    course_url = f"sqlite:///{course_path}"
    course_session_factory = _make_course_db(course_path)

    with app_env["PlatformSessionLocal"]() as platform_db:
        lecturer = _create_lecturer(platform_db, slug="auditreport", course_db_url=course_url)

    _, _, attempt_ids = _seed_exam_with_attempts(course_session_factory, student_count=1, submitted=True)
    attempt_id = attempt_ids[0]

    app = __import__("app.main", fromlist=["app"]).app
    admin_client = TestClient(app, follow_redirects=False)
    _set_admin_cookie(admin_client, slug="auditreport", lecturer_id=lecturer.id)

    reviewed = admin_client.post(
        f"/auditreport/admin/attempts/{attempt_id}/mark-reviewed",
        data={"review_notes": "Checked against incident log."},
    )
    assert reviewed.status_code == 303
    assert reviewed.headers["location"].endswith("?reviewed=1")

    report = admin_client.get(f"/auditreport/admin/attempts/{attempt_id}/audit-report")
    assert report.status_code == 200
    assert report.headers["content-disposition"].startswith("attachment;")

    body = report.json()
    assert body["attempt_id"] == attempt_id
    assert body["review"]["reviewed_by"] == lecturer.email
    assert body["review"]["review_notes"] == "Checked against incident log."
    assert any(item["action"] == "review" for item in body["history"])


def test_exam_level_consolidated_audit_export_json_and_csv(app_env):
    course_path = app_env["tmp_path"] / "exam_audit_export.sqlite3"
    course_url = f"sqlite:///{course_path}"
    course_session_factory = _make_course_db(course_path)

    with app_env["PlatformSessionLocal"]() as platform_db:
        lecturer = _create_lecturer(platform_db, slug="examreport", course_db_url=course_url)

    exam_id, _, attempt_ids = _seed_exam_with_attempts(course_session_factory, student_count=2, submitted=True)

    app = __import__("app.main", fromlist=["app"]).app
    admin_client = TestClient(app, follow_redirects=False)
    _set_admin_cookie(admin_client, slug="examreport", lecturer_id=lecturer.id)

    # Create workflow history so export contains non-empty audit aggregates.
    compare = admin_client.post(f"/examreport/admin/attempts/{attempt_ids[0]}/compare-pdf")
    assert compare.status_code == 303
    reviewed = admin_client.post(
        f"/examreport/admin/attempts/{attempt_ids[1]}/mark-reviewed",
        data={"review_notes": "Reviewed from exam export test."},
    )
    assert reviewed.status_code == 303

    json_report = admin_client.get(f"/examreport/admin/exams/{exam_id}/audit-report.json")
    assert json_report.status_code == 200
    assert json_report.headers["content-disposition"].startswith("attachment;")
    payload = json_report.json()
    assert payload["exam"]["id"] == exam_id
    assert payload["summary"]["attempt_count"] == 2
    assert len(payload["attempts"]) == 2
    assert {item["attempt_id"] for item in payload["attempts"]} == set(attempt_ids)
    assert any(item["reviewed_by"] == lecturer.email for item in payload["attempts"])

    csv_report = admin_client.get(f"/examreport/admin/exams/{exam_id}/audit-report.csv")
    assert csv_report.status_code == 200
    assert csv_report.headers["content-disposition"].startswith("attachment;")
    rows = list(csv.DictReader(StringIO(csv_report.text)))
    assert len(rows) == 2
    assert {int(row["attempt_id"]) for row in rows} == set(attempt_ids)
    assert "regeneration_count" in rows[0]
    assert "compare_mismatch_count" in rows[0]


def test_exam_level_download_all_submission_pdfs_zip(app_env):
    course_path = app_env["tmp_path"] / "exam_all_pdfs.sqlite3"
    course_url = f"sqlite:///{course_path}"
    course_session_factory = _make_course_db(course_path)

    with app_env["PlatformSessionLocal"]() as platform_db:
        lecturer = _create_lecturer(platform_db, slug="allpdfs", course_db_url=course_url)

    exam_id, _, attempt_ids = _seed_exam_with_attempts(course_session_factory, student_count=2, submitted=True)

    app = __import__("app.main", fromlist=["app"]).app
    admin_client = TestClient(app, follow_redirects=False)
    _set_admin_cookie(admin_client, slug="allpdfs", lecturer_id=lecturer.id)

    response = admin_client.get(f"/allpdfs/admin/exams/{exam_id}/submission-pdfs.zip")
    assert response.status_code == 200
    assert response.headers["content-disposition"].startswith("attachment;")
    assert response.headers["content-type"].startswith("application/zip")

    with ZipFile(BytesIO(response.content)) as archive:
        names = archive.namelist()
        assert len(names) == 2
        for attempt_id in attempt_ids:
            assert any(name.startswith(f"attempt_{attempt_id}_") for name in names)
        for name in names:
            content = archive.read(name)
            assert content.startswith(b"%PDF")


def test_submissions_page_filters_by_exam_tab(app_env):
    course_path = app_env["tmp_path"] / "attempt_tabs.sqlite3"
    course_url = f"sqlite:///{course_path}"
    course_session_factory = _make_course_db(course_path)

    with app_env["PlatformSessionLocal"]() as platform_db:
        lecturer = _create_lecturer(platform_db, slug="tabs", course_db_url=course_url)

    exam1_id, _, _ = _seed_exam_with_attempts(course_session_factory, student_count=1, submitted=True)
    with course_session_factory() as db:
        exam2 = Exam(
            title="Second Exam",
            duration_minutes=60,
            total_marks=50,
            section_c_required=0,
            is_open=True,
            show_submission_pdf=True,
        )
        db.add(exam2)
        db.flush()
        q = Question(exam_id=exam2.id, section="B", order_index=1, prompt="Explain B")
        db.add(q)
        db.flush()
        student = Student(
            full_name="Second Student",
            email="second@student.test",
            computer_number="C900",
            password_hash=hash_password("student-pass"),
            is_verified=True,
            is_approved=True,
        )
        db.add(student)
        db.flush()
        attempt = Attempt(
            exam_id=exam2.id,
            student_id=student.id,
            started_at=datetime.utcnow() - timedelta(minutes=5),
            deadline_at=datetime.utcnow() + timedelta(minutes=55),
            is_locked=True,
            submitted_at=datetime.utcnow(),
            submission_mode="manual",
        )
        db.add(attempt)
        db.flush()
        db.add(Answer(attempt_id=attempt.id, question_id=q.id, value="B answer", selected=False))
        db.refresh(attempt, attribute_names=["student", "exam", "answers", "incidents"])
        attempt.pdf_bytes = pdf.build(attempt, {a.question_id: a for a in attempt.answers}, lecturer_stub("Second"))
        attempt.pdf_filename = f"seed_{attempt.id}.pdf"
        db.commit()

    app = __import__("app.main", fromlist=["app"]).app
    admin_client = TestClient(app, follow_redirects=False)
    _set_admin_cookie(admin_client, slug="tabs", lecturer_id=lecturer.id)

    filtered = admin_client.get(f"/tabs/admin/attempts?exam_id={exam1_id}")
    assert filtered.status_code == 200
    assert "Operations Test" in filtered.text
    assert "Second Exam" in filtered.text  # appears in tabs
    assert "Second Student" not in filtered.text  # should not appear in table rows
