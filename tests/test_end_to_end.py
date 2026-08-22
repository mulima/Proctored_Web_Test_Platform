"""End-to-end exercise of the test platform.

Drives the whole journey against a real database and a real ASGI app: a student
registers, confirms their email, an admin builds and opens a paper, the student sits it,
trips the proctoring rules, and submits. Asserts on what actually reaches the marker.

    python3 tests/test_end_to_end.py
"""

import base64
import io
import os
import sys
import tempfile

WORK_DIR = tempfile.mkdtemp(prefix="testplatform_e2e_")
DB_PATH = os.path.join(WORK_DIR, "test.db")

os.environ.update(
    {
        "DATABASE_URL": f"sqlite:///{DB_PATH}",
        "SECRET_KEY": "test-secret-key-not-for-production-0123456789abcdef",
        "ADMIN_EMAIL": "lecturer@unza.zm",
        "ADMIN_PASSWORD": "test-admin-password",
        "MAIL_BACKEND": "console",
        "BASE_URL": "http://testserver",
        "ABSENCE_FLAG_SECONDS": "60",
        "STRIKE_FLAG_AFTER": "3",
        "SNAPSHOT_ALERT_MIN_INTERVAL_SECONDS": "0",
    }
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402

alembic_config = Config(os.path.join(ROOT, "alembic.ini"))
alembic_config.set_main_option("script_location", os.path.join(ROOT, "alembic"))
command.upgrade(alembic_config, "head")

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.main import app, ensure_admin  # noqa: E402
from app.models import Attempt, Incident, Snapshot, Student  # noqa: E402

failures = []
checks = 0


def check(label, condition, detail=""):
    global checks
    checks += 1
    print(f"  {'PASS' if condition else 'FAIL'}  {label} {'' if condition else detail}")
    if not condition:
        failures.append(label)


TINY_JPEG = base64.b64encode(
    bytes.fromhex(
        "ffd8ffe000104a46494600010100000100010000ffdb004300ffffffffffffffffffffffffffff"
        "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
        "ffffffffffffffffffffffffffffffffffc00011080001000103012200021101031101ffc40014"
        "0001000000000000000000000000000000009fffc4001401010000000000000000000000000000"
        "0000ffda000c03010002110311003f00bf8001ffd9"
    )
).decode()


def main():
    ensure_admin()
    client = TestClient(app, follow_redirects=False)
    admin = TestClient(app, follow_redirects=False)

    print("\n1. Public pages render")
    check("register page loads", client.get("/register").status_code == 200)
    check("login page loads", client.get("/login").status_code == 200)
    check("privacy notice loads", client.get("/privacy").status_code == 200)
    check("health check passes", client.get("/healthz").json().get("ok") is True)
    check("signed-out student is redirected", client.get("/").status_code == 303)

    print("\n2. Registration validates before it accepts")
    bad = client.post(
        "/register",
        data={
            "full_name": "X",
            "email": "not-an-email",
            "computer_number": "1",
            "password": "short",
            "confirm_password": "different",
        },
    )
    check("bad registration is rejected", bad.status_code == 400, str(bad.status_code))
    body = bad.text
    check("it explains the name", "full name" in body.lower())
    check("it explains the email", "valid email" in body.lower())
    check("it explains the mismatch", "do not match" in body.lower())

    print("\n3. A student registers and must confirm their email")
    response = client.post(
        "/register",
        data={
            "full_name": "Chanda Mulenga",
            "email": "chanda@unza.zm",
            "computer_number": "2026123456",
            "password": "a-good-long-password",
            "confirm_password": "a-good-long-password",
        },
    )
    check("registration accepted", response.status_code == 200, str(response.status_code))
    check("told to check email", "check your email" in response.text.lower())

    with SessionLocal() as db:
        student = db.scalar(select(Student).where(Student.email == "chanda@unza.zm"))
        check("student row created", student is not None)
        check("not verified yet", student.is_verified is False)
        check("computer number normalised", student.computer_number == "2026123456")
        token = student.verification_token
        check("verification token issued", bool(token))

    blocked = client.post("/login", data={"email": "chanda@unza.zm", "password": "a-good-long-password"})
    check("cannot sign in before confirming", blocked.status_code == 403, str(blocked.status_code))
    check("told to confirm first", "confirm your email" in blocked.text.lower())

    print("\n4. The verification link works, once, and is safe to click twice")
    verified = client.get(f"/verify?token={token}")
    check("link accepted", verified.status_code == 200)
    check("confirmed message shown", "confirmed" in verified.text.lower())

    with SessionLocal() as db:
        student = db.scalar(select(Student).where(Student.email == "chanda@unza.zm"))
        check("account marked verified", student.is_verified is True)
        # The property that matters: the stored token is gone, so the link in the inbox
        # can no longer verify anything. Clicking it again is merely idempotent.
        check("stored token cleared after use", student.verification_token is None)

    replay = client.get(f"/verify?token={token}")
    check("clicking the link twice is not an error", replay.status_code == 200, str(replay.status_code))
    check("second click says already confirmed", "already confirmed" in replay.text.lower())
    check("bad token rejected", client.get("/verify?token=rubbish").status_code == 400)

    # A superseded token must not verify an account that is still waiting. Register a
    # throwaway, take its first token, force a second to be issued, then replay the first.
    stale_client = TestClient(app, follow_redirects=False)
    stale_client.post(
        "/register",
        data={
            "full_name": "Stale Token",
            "email": "stale@unza.zm",
            "computer_number": "2026777777",
            "password": "stale-long-password",
            "confirm_password": "stale-long-password",
        },
    )
    with SessionLocal() as db:
        stale = db.scalar(select(Student).where(Student.email == "stale@unza.zm"))
        first_token = stale.verification_token
    stale_client.post("/resend", data={"email": "stale@unza.zm"})
    superseded = stale_client.get(f"/verify?token={first_token}")
    check("a superseded link cannot verify", superseded.status_code == 400, str(superseded.status_code))
    with SessionLocal() as db:
        stale = db.scalar(select(Student).where(Student.email == "stale@unza.zm"))
        check("that account is still unverified", stale.is_verified is False)

    print("\n5. Sign in works once confirmed")
    login = client.post(
        "/login", data={"email": "chanda@unza.zm", "password": "a-good-long-password"}
    )
    check("login redirects to the dashboard", login.status_code == 303, str(login.status_code))
    dashboard = client.get("/")
    check("dashboard loads", dashboard.status_code == 200)
    check("no test open yet", "no test is open" in dashboard.text.lower())
    check(
        "wrong password refused",
        TestClient(app).post(
            "/login", data={"email": "chanda@unza.zm", "password": "wrong"}
        ).status_code
        == 401,
    )

    print("\n6. Admin signs in with the password set at start-up")
    check(
        "wrong admin password refused",
        admin.post("/admin/login", data={"email": "lecturer@unza.zm", "password": "nope"}).status_code
        == 401,
    )
    admin_login = admin.post(
        "/admin/login", data={"email": "lecturer@unza.zm", "password": "test-admin-password"}
    )
    check("admin login redirects", admin_login.status_code == 303, str(admin_login.status_code))
    check("admin home loads", admin.get("/admin").status_code == 200)
    check("student roster visible", "Chanda Mulenga" in admin.get("/admin/students").text)

    print("\n7. Admin builds a paper")
    admin.post(
        "/admin/exams",
        data={
            "title": "First Semester Test 1",
            "duration_minutes": "90",
            "total_marks": "100",
            "section_c_required": "2",
        },
    )
    exam_id = 1
    payload = (
        '{"multiple_choice":[{"question":"Which is e-business but not e-commerce?",'
        '"options":["Selling online","Digitising procurement","Neither","Both"],"marks":2},'
        '{"question":"A platform gets more useful as users join. This is:",'
        '"options":["Scale","A network effect","The long tail","Churn"],"marks":2}],'
        '"short_answer":["Distinguish e-business from e-commerce. (5 marks)",'
        '"Why does gross margin cap acquisition spend? (5 marks)"],'
        '"long_writeup":[{"title":"Question 1: Ride-hailing","prompt":"Analyse the Lusaka market.","marks":25},'
        '{"title":"Question 2: Retail strategy","prompt":"Marketplace or first-party?","marks":25},'
        '{"title":"Question 3: Infrastructure","prompt":"Redesign under constraints.","marks":25}]}'
    )
    imported = admin.post(f"/admin/exams/{exam_id}/import", data={"payload": payload})
    check("import redirects back", imported.status_code == 303)
    exam_page = admin.get(f"/admin/exams/{exam_id}")
    check("questions listed", "Which is e-business" in exam_page.text)
    check("section C listed", "Ride-hailing" in exam_page.text)

    print("\n8. An exam must be opened before anyone can sit it")
    check("student still sees nothing", "no test is open" in client.get("/").text.lower())
    admin.post(f"/admin/exams/{exam_id}/open")
    dashboard = client.get("/")
    check("paper now offered", "First Semester Test 1" in dashboard.text)
    check("start button shown", "Start the test" in dashboard.text)

    print("\n9. The student sits the paper")
    start = client.post("/start")
    check("start redirects to the paper", start.status_code == 303)
    sit = client.get("/sit")
    check("sitting page loads", sit.status_code == 200)
    check("questions inlined", "network effect" in sit.text)
    check("blackout present", 'id="blackout"' in sit.text)
    check("preflight gate present", 'id="preflight"' in sit.text)

    with SessionLocal() as db:
        attempt = db.scalar(select(Attempt))
        check("attempt created", attempt is not None)
        attempt_id = attempt.id
        question_ids = [q.id for q in sorted(attempt.exam.questions, key=lambda q: (q.section, q.order_index))]

    saved = client.post(
        "/api/save",
        json={
            "current_question": 3,
            "answers": [
                {"question_id": question_ids[0], "value": "B", "selected": False},
                {"question_id": question_ids[1], "value": "B", "selected": False},
                {"question_id": question_ids[2], "value": "A short answer about e-business.", "selected": False},
                {"question_id": question_ids[3], "value": "Margin funds acquisition.", "selected": False},
                {"question_id": question_ids[4], "value": "Long answer one.", "selected": True},
                {"question_id": question_ids[5], "value": "Long answer two.", "selected": True},
            ],
        },
    )
    check("answers saved", saved.status_code == 200, saved.text[:200])
    check("status returned with save", "remaining_seconds" in saved.json())

    print("\n10. Proctoring incidents are recorded and counted")
    client.post("/api/incident", json={"category": "FULLSCREEN_EXIT", "detail": "Left full screen."})
    client.post("/api/incident", json={"category": "COPY_BLOCKED", "detail": "Copy blocked."})
    phone = client.post(
        "/api/incident",
        json={
            "category": "PHONE_DETECTED",
            "detail": "A phone-like device was visible.",
            "snapshot": "data:image/jpeg;base64," + TINY_JPEG,
        },
    )
    check("incident accepted", phone.status_code == 200, phone.text[:200])
    status = phone.json()
    check("two strikeable incidents counted", status["strike_count"] == 2, str(status["strike_count"]))
    check("not flagged at 2 of 3", status["flagged"] is False)

    with SessionLocal() as db:
        incidents = db.scalars(select(Incident).where(Incident.attempt_id == attempt_id)).all()
        categories = [i.category for i in incidents]
        check("all three events stored", len(incidents) == 3, str(categories))
        check("copy block recorded but not counted",
              next(i for i in incidents if i.category == "COPY_BLOCKED").counted is False)
        snapshots = db.scalars(select(Snapshot).where(Snapshot.attempt_id == attempt_id)).all()
        check("snapshot stored against the incident", len(snapshots) == 1, str(len(snapshots)))
        check("snapshot has bytes", len(snapshots[0].image) > 50)
        check("snapshot linked to the incident", snapshots[0].incident_id is not None)
        snapshot_id = snapshots[0].id

    third = client.post("/api/incident", json={"category": "SCREENSHOT_KEY", "detail": "PrintScreen."})
    check("third strike flags the attempt", third.json()["flagged"] is True)

    print("\n11. Submission is refused until Section C is right")
    with SessionLocal() as db:
        attempt = db.get(Attempt, attempt_id)
        for answer in attempt.answers:
            if answer.question.section == "C":
                answer.selected = False
        db.commit()
    refused = client.post("/api/submit", json={})
    check("submit refused with none selected", refused.status_code == 400, str(refused.status_code))
    check("the message says how many", "exactly 2" in refused.json()["error"], refused.json()["error"])

    client.post(
        "/api/save",
        json={
            "current_question": 5,
            "answers": [
                {"question_id": question_ids[4], "value": "Long answer one.", "selected": True},
                {"question_id": question_ids[5], "value": "Long answer two.", "selected": True},
            ],
        },
    )

    print("\n12. Submitting produces a PDF and locks the paper")
    submitted = client.post("/api/submit", json={})
    check("submit accepted", submitted.status_code == 200, submitted.text[:300])
    check("client told it is locked", submitted.json().get("locked") is True)

    with SessionLocal() as db:
        attempt = db.get(Attempt, attempt_id)
        check("attempt locked in the database", attempt.is_locked is True)
        check("submitted timestamp set", attempt.submitted_at is not None)
        check("PDF stored", attempt.pdf_bytes is not None and len(attempt.pdf_bytes) > 3000,
              str(len(attempt.pdf_bytes or b"")))
        check("PDF is a PDF", (attempt.pdf_bytes or b"")[:4] == b"%PDF")
        check("filename carries the computer number", "2026123456" in attempt.pdf_filename,
              attempt.pdf_filename)
        check("mode recorded", attempt.submission_mode == "manual", attempt.submission_mode)

    again = client.post("/api/submit", json={})
    check("second submission refused", again.status_code == 409, str(again.status_code))
    check("save refused after locking", client.post("/api/save", json={"answers": []}).status_code == 409)

    print("\n13. The student and the admin can both retrieve the paper")
    own = client.get("/my-submission.pdf")
    check("student can open their own PDF", own.status_code == 200 and own.content[:4] == b"%PDF")
    admin_pdf = admin.get(f"/admin/attempts/{attempt_id}/pdf")
    check("admin can open the PDF", admin_pdf.status_code == 200 and admin_pdf.content[:4] == b"%PDF")
    image = admin.get(f"/admin/snapshots/{snapshot_id}")
    check("admin can view the snapshot", image.status_code == 200 and len(image.content) > 50)
    check("signed-out user cannot view a snapshot",
          TestClient(app, follow_redirects=False).get(f"/admin/snapshots/{snapshot_id}").status_code == 303)

    print("\n14. The admin sees the whole record")
    detail = admin.get(f"/admin/attempts/{attempt_id}")
    check("attempt page loads", detail.status_code == 200)
    check("flagged banner shown", "flagged for review" in detail.text.lower())
    check("incident log rendered", "Left full-screen test mode" in detail.text)
    check("evidence section rendered", "Evidence snapshots" in detail.text)
    check("answers shown to the marker", "Long answer one." in detail.text)
    check("flagged filter finds it", "2026123456" in admin.get("/admin/attempts?flagged=1").text)

    print("\n15. The application log is durable and queryable")
    logs = admin.get("/admin/logs?limit=500")
    check("log page loads", logs.status_code == 200)
    for event in ("REGISTERED", "EMAIL_VERIFIED", "LOGIN", "ATTEMPT_STARTED", "SUBMITTED"):
        check(f"{event} logged", event in logs.text)
    check("incident logged too", "INCIDENT_PHONE_DETECTED" in logs.text)
    check("email attempts logged", "SUBMISSION_EMAIL" in logs.text)
    check("log filter works", "ADMIN_LOGIN" in admin.get("/admin/logs?q=admin_login").text)

    print("\n16. A second student cannot reach the first one's work")
    other = TestClient(app, follow_redirects=False)
    other.post(
        "/register",
        data={
            "full_name": "Bwalya Phiri",
            "email": "bwalya@unza.zm",
            "computer_number": "2026999999",
            "password": "another-long-password",
            "confirm_password": "another-long-password",
        },
    )
    with SessionLocal() as db:
        second = db.scalar(select(Student).where(Student.email == "bwalya@unza.zm"))
        other.get(f"/verify?token={second.verification_token}")
    other.post("/login", data={"email": "bwalya@unza.zm", "password": "another-long-password"})
    check("second student cannot open the admin panel", other.get("/admin").status_code == 303)
    check("second student gets no PDF", other.get("/my-submission.pdf").status_code == 404)
    check("second student sees the open paper", "First Semester Test 1" in other.get("/").text)

    print("\n17. Duplicate registration is handled without leaking who exists")
    duplicate = TestClient(app).post(
        "/register",
        data={
            "full_name": "Someone Else",
            "email": "chanda@unza.zm",
            "computer_number": "2026000001",
            "password": "yet-another-password",
            "confirm_password": "yet-another-password",
        },
    )
    check("duplicate email rejected", duplicate.status_code == 400)
    check("told to sign in instead", "already exists" in duplicate.text.lower())
    reused_number = TestClient(app).post(
        "/register",
        data={
            "full_name": "Third Person",
            "email": "third@unza.zm",
            "computer_number": "2026123456",
            "password": "third-long-password",
            "confirm_password": "third-long-password",
        },
    )
    check("duplicate computer number rejected", reused_number.status_code == 400)
    check("resend never reveals membership",
          "if that address" in TestClient(app).post("/resend", data={"email": "nobody@unza.zm"}).text.lower())

    print("\n18. With no course configured, nothing course-specific leaks")
    # This suite deliberately sets no COURSE_CODE, COURSE_TITLE or INSTITUTION, so every
    # page here is running on the generic defaults.
    from app.config import settings as live_settings

    check("no course code is configured", live_settings.course_code == "",
          repr(live_settings.course_code))
    check("the brand falls back to the app name",
          live_settings.brand == live_settings.app_name, live_settings.brand)
    check("the subtitle describes the platform, not a course",
          live_settings.subtitle == "Proctored online assessment", live_settings.subtitle)
    check("filenames get a sensible prefix", live_settings.file_prefix == "exam",
          live_settings.file_prefix)

    for path in ("/login", "/register", "/privacy"):
        page = TestClient(app).get(path).text
        check(f"{path} names no course", "MBS6011" not in page and "E-Business" not in page)
        check(f"{path} shows the platform name", live_settings.app_name in page)

    with SessionLocal() as db:
        stored = db.get(Attempt, attempt_id)
        check("the PDF filename does not start with a stray underscore",
              not stored.pdf_filename.startswith("_"), stored.pdf_filename)
        check("the PDF filename uses the generic prefix",
              stored.pdf_filename.startswith("exam_"), stored.pdf_filename)

    # And when a course IS configured, it shows up everywhere it should.
    live_settings.course_code = "ABC1234"
    live_settings.course_title = "ABC1234: Some Other Course"
    live_settings.institution = "Some Other University"
    try:
        page = TestClient(app).get("/login").text
        check("a configured course code reaches the page", "ABC1234" in page)
        check("a configured institution reaches the footer", "Some Other University" in page)
        check("the brand switches to the course code", live_settings.brand == "ABC1234")
        check("a password built from the course code is refused",
              any("course code" in problem for problem in
                  __import__("app.security", fromlist=["x"]).password_problems("abc1234pass")),
              "weak-password rule did not fire")
    finally:
        live_settings.course_code = ""
        live_settings.course_title = ""
        live_settings.institution = ""

    print(f"\n{checks - len(failures)} of {checks} checks passed")
    if failures:
        print("FAILURES:\n  - " + "\n  - ".join(failures))
        return 1
    return 0


if __name__ == "__main__":
    code = main()
    print(f"\nDatabase left at {DB_PATH}")
    sys.exit(code)
