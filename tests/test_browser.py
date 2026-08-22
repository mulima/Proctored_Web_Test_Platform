"""Drives the sitting page in a real Chromium against a scripted fake webcam.

This container's Chromium exposes no capture devices, so tests/fake_camera.js replaces
getUserMedia with a canvas captureStream whose pixels the test controls. That is a better
harness than a synthetic device anyway: the presence rules can be driven deliberately by
switching the scene, rather than hoping a moving pattern happens to trip them.

The ONNX phone detector is not exercised here - no model file ships in the repo - so the
page is asserted to say so honestly rather than to pretend detection is running.

    python3 tests/test_browser.py
"""

import os
import socket
import sys
import tempfile
import threading
import time

WORK_DIR = tempfile.mkdtemp(prefix="testplatform_browser_")
DB_PATH = os.path.join(WORK_DIR, "browser.db")
os.environ.update(
    {
        "DATABASE_URL": f"sqlite:///{DB_PATH}",
        "SECRET_KEY": "browser-test-secret-key-0123456789abcdefghij",
        "ADMIN_EMAIL": "lecturer@unza.zm",
        "ADMIN_PASSWORD": "test-admin-password",
        "MAIL_BACKEND": "console",
        "FULLSCREEN_GRACE_SECONDS": "1",
        "HIDDEN_WARN_SECONDS": "1",
        "ABSENCE_FLAG_SECONDS": "3",
        "ABSENCE_WARN_SECONDS": "1",
    }
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402

alembic_config = Config(os.path.join(ROOT, "alembic.ini"))
alembic_config.set_main_option("script_location", os.path.join(ROOT, "alembic"))
command.upgrade(alembic_config, "head")

import uvicorn  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.main import app, ensure_admin  # noqa: E402
from app.models import Attempt, Exam, Incident, Question, Snapshot, Student  # noqa: E402
from app.security import hash_password  # noqa: E402

CHROME = "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"

failures = []
checks = 0


def check(label, condition, detail=""):
    global checks
    checks += 1
    print(f"  {'PASS' if condition else 'FAIL'}  {label} {'' if condition else detail}")
    if not condition:
        failures.append(label)


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def seed():
    """A verified student and an open one-question-per-section paper."""
    ensure_admin()
    with SessionLocal() as db:
        student = Student(
            full_name="Chanda Mulenga",
            email="chanda@unza.zm",
            computer_number="2026123456",
            password_hash=hash_password("a-good-long-password"),
            is_verified=True,
            is_approved=True,
        )
        db.add(student)
        exam = Exam(title="Browser Test Paper", duration_minutes=90, section_c_required=1, is_open=True)
        db.add(exam)
        db.flush()
        db.add(Question(exam_id=exam.id, section="A", order_index=1,
                        prompt="Which is a network effect?",
                        options=["Scale economies", "Value rises with users", "Churn", "Margin"], marks=2))
        db.add(Question(exam_id=exam.id, section="B", order_index=1,
                        prompt="Distinguish e-business from e-commerce.", marks=5))
        db.add(Question(exam_id=exam.id, section="C", order_index=1,
                        title="Question 1: Ride-hailing", prompt="Analyse the Lusaka market.", marks=25))
        db.commit()


def incidents_of(category=None):
    with SessionLocal() as db:
        query = select(Incident)
        if category:
            query = query.where(Incident.category == category)
        return db.scalars(query).all()


def main():
    seed()
    port = free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(60):
        if server.started:
            break
        time.sleep(0.15)
    base = f"http://127.0.0.1:{port}"

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=CHROME,
            args=["--no-sandbox", "--use-fake-ui-for-media-stream"],
        )
        context = browser.new_context(
            viewport={"width": 1280, "height": 800},
            screen={"width": 1920, "height": 1080},
            permissions=["camera"],
            base_url=base,
        )
        context.add_init_script(
            path=os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_camera.js")
        )
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))

        print("\n1. Sign in and reach the paper")
        page.goto(base + "/login")
        page.fill('input[name="email"]', "chanda@unza.zm")
        page.fill('input[name="password"]', "a-good-long-password")
        page.click('button[type="submit"]')
        page.wait_for_load_state("networkidle")
        check("landed on the dashboard", "Browser Test Paper" in page.content())
        page.click('form[action="/start"] button')
        page.wait_for_load_state("networkidle")
        check("sitting page reached", page.url.endswith("/sit"), page.url)

        print("\n2. The paper is gated behind a preflight, not shown immediately")
        check("preflight overlay visible", page.is_visible("#preflight"))
        check(
            "begin button becomes enabled",
            page.wait_for_function("() => !document.getElementById('beginButton').disabled",
                                   timeout=10000) is not None,
        )
        check("no uncaught JavaScript errors", not errors, "; ".join(errors[:3]))

        print("\n3. Beginning starts the camera and renders question one")
        page.click("#beginButton")
        page.wait_for_timeout(2500)
        check("preflight dismissed", not page.is_visible("#preflight"))
        check("first question rendered", "network effect" in page.inner_text("#questionPage").lower())
        camera_chip = page.inner_text("#cameraChip")
        check("camera reported active", "active" in camera_chip.lower(), camera_chip)
        check("a real stream is attached",
              page.evaluate("() => !!document.getElementById('proctorVideo').srcObject"))
        detector_chip = page.inner_text("#detectorChip")
        # No ONNX model ships in the repo, so the honest answer here is "unavailable".
        check("detector state reported honestly", "detector" in detector_chip.lower(), detector_chip)

        print("\n4. Begin puts the paper into full screen, so nothing is blacked out")
        check("browser is in full screen",
              page.evaluate("() => !!document.fullscreenElement"))
        check("no blackout while full screen",
              page.eval_on_selector("#blackout", "el => el.style.display") != "flex")
        check("paper is readable", "blanked" not in page.eval_on_selector("body", "el => el.className"))

        print("\n5. Leaving full screen blacks the paper out and is recorded once")
        page.evaluate("() => document.exitFullscreen()")   # what pressing Escape does
        page.wait_for_timeout(700)
        check("blackout showing", page.eval_on_selector("#blackout", "el => el.style.display") == "flex")
        check("shield covers the paper", "blanked" in page.eval_on_selector("body", "el => el.className"))
        check("candidate is told why",
              "full-screen" in page.inner_text("#blackoutMessage").lower(),
              page.inner_text("#blackoutMessage"))
        page.wait_for_timeout(2000)   # past the 1s grace period this test configures
        check("FULLSCREEN_EXIT recorded", len(incidents_of("FULLSCREEN_EXIT")) >= 1,
              str([i.category for i in incidents_of()]))
        check("reported once, not every tick", len(incidents_of("FULLSCREEN_EXIT")) == 1,
              str(len(incidents_of("FULLSCREEN_EXIT"))))

        print("\n5b. Returning to full screen restores the paper")
        page.evaluate("() => document.documentElement.requestFullscreen()")
        page.wait_for_timeout(900)
        check("blackout cleared", page.eval_on_selector("#blackout", "el => el.style.display") == "none")
        check("paper readable again", "blanked" not in page.eval_on_selector("body", "el => el.className"))

        print("\n6. Answers save to the server as they are typed")
        page.click('input[name="mc"][value="B"]')
        page.wait_for_timeout(800)
        page.click("#nextBtn")
        page.wait_for_timeout(1200)
        page.fill("#answerBox", "E-business is the wider set of digitised processes.")
        page.wait_for_timeout(6000)  # the autosave runs every five seconds
        with SessionLocal() as db:
            attempt = db.scalar(select(Attempt))
            values = {a.question.section: a.value for a in attempt.answers}
        check("multiple choice answer stored", values.get("A") == "B", str(values))
        check("short answer stored", "wider set" in (values.get("B") or ""), str(values))
        check("save indicator shows saved", "saved" in page.inner_text("#saveChip").lower(),
              page.inner_text("#saveChip"))

        print("\n7. Blocked keys and copy are refused and recorded")
        page.keyboard.press("Control+p")
        page.keyboard.press("F12")
        page.keyboard.press("Control+c")
        page.mouse.click(400, 300, button="right")
        page.wait_for_timeout(900)
        categories = [i.category for i in incidents_of()]
        check("shortcut blocks recorded", "SHORTCUT_BLOCKED" in categories, str(categories))
        check("copy block recorded", "COPY_BLOCKED" in categories, str(categories))
        check("right-click block recorded", "CONTEXT_MENU_BLOCKED" in categories, str(categories))

        print("\n8. Blocked keys are noise, not strikes")
        with SessionLocal() as db:
            attempt = db.scalar(select(Attempt))
            counted = {i.category for i in attempt.incidents if i.counted}
            strike_count = attempt.strike_count
        check("SHORTCUT_BLOCKED does not count", "SHORTCUT_BLOCKED" not in counted, str(counted))
        check("COPY_BLOCKED does not count", "COPY_BLOCKED" not in counted, str(counted))
        check("FULLSCREEN_EXIT does count", "FULLSCREEN_EXIT" in counted, str(counted))
        check("strike counter matches", strike_count == len(counted), f"{strike_count} vs {counted}")
        check("counter visible to the candidate", "Incidents recorded" in page.inner_text("#strikeChip"),
              page.inner_text("#strikeChip"))

        print("\n8b. An empty chair is warned about, then recorded")
        page.evaluate("() => window.__fakeCam.setScene('empty')")
        page.wait_for_timeout(2000)
        check("candidate is warned on screen",
              "not visible to the camera" in page.inner_text("#alerts").lower(),
              page.inner_text("#alerts"))
        check("a brief absence is not yet an incident", not incidents_of("ABSENCE_FLAGGED"),
              str(len(incidents_of("ABSENCE_FLAGGED"))))
        page.wait_for_timeout(3500)   # past ABSENCE_FLAG_SECONDS=3
        check("sustained absence is recorded", len(incidents_of("ABSENCE_FLAGGED")) >= 1,
              str([i.category for i in incidents_of()]))
        with SessionLocal() as db:
            snaps = db.scalars(select(Snapshot)).all()
        check("an evidence snapshot was captured", len(snaps) >= 1, str(len(snaps)))
        check("the snapshot holds real bytes", bool(snaps) and len(snaps[0].image) > 200,
              str(len(snaps[0].image) if snaps else 0))
        check("it is linked to the incident", bool(snaps) and snaps[0].incident_id is not None)
        check("absence counts as a strike",
              any(i.counted for i in incidents_of("ABSENCE_FLAGGED")))

        page.evaluate("() => window.__fakeCam.setScene('person')")
        page.wait_for_timeout(1800)
        check("warning clears when the candidate returns",
              "not visible to the camera" not in page.inner_text("#alerts").lower(),
              page.inner_text("#alerts"))
        check("returning does not add another incident", len(incidents_of("ABSENCE_FLAGGED")) == 1,
              str(len(incidents_of("ABSENCE_FLAGGED"))))

        print("\n8c. The prefilter gates the expensive detector")
        stats = page.evaluate("""() => {
          const pf = new window.MBSProctor.Prefilter({ maxSkipSeconds: 999 });
          const W = 80, H = 60, N = W * H;
          const quiet = () => { const g = new Float32Array(N); for (let i = 0; i < N; i++) g[i] = 70 + (i % 3); return g; };
          const first = pf.shouldRunModel(quiet());
          let skipped = 0;
          for (let i = 0; i < 12; i++) if (!pf.shouldRunModel(quiet()).run) skipped++;
          const moved = new Float32Array(N).fill(200);
          const motion = pf.shouldRunModel(moved);
          const pf2 = new window.MBSProctor.Prefilter({ maxSkipSeconds: 999 });
          pf2.shouldRunModel(quiet()); pf2.shouldRunModel(quiet());
          const lit = quiet();
          for (let y = 20; y < 44; y++) for (let x = 30; x < 42; x++) lit[y * W + x] = 250;
          const bright = pf2.shouldRunModel(lit);
          const pf3 = new window.MBSProctor.Prefilter({ maxSkipSeconds: 0 });
          pf3.shouldRunModel(quiet());
          const watchdog = pf3.shouldRunModel(quiet());
          const t0 = performance.now();
          const pf4 = new window.MBSProctor.Prefilter({ maxSkipSeconds: 999 });
          for (let i = 0; i < 40; i++) pf4.shouldRunModel(quiet());
          return { first: first.run, skipped, motion, bright, watchdog,
                   msPerFrame: (performance.now() - t0) / 40 };
        }""")
        check("the first frame always runs", stats["first"] is True)
        check("a still scene skips inference", stats["skipped"] >= 10, str(stats["skipped"]))
        check("motion wakes the detector", stats["motion"]["run"] is True, str(stats["motion"]))
        check("and says why", stats["motion"]["reason"].startswith("motion"), stats["motion"]["reason"])
        check("a lit rectangle wakes it", stats["bright"]["run"] is True, str(stats["bright"]))
        check("the watchdog forces a run", stats["watchdog"]["reason"] == "watchdog",
              str(stats["watchdog"]))
        check("it is cheap enough to run four times a second",
              stats["msPerFrame"] < 5.0, f"{stats['msPerFrame']:.2f}ms")
        print(f"        ({stats['msPerFrame']:.2f} ms per frame in-browser)")

        print("\n9. Section C must be selected before the paper will submit")
        page.click("#nextBtn")
        page.wait_for_timeout(800)
        page.fill("#answerBox", "Ulendo, Yango, Bolt and inDrive all multi-home their drivers.")
        page.wait_for_timeout(600)
        page.click("#submitBtn")
        page.wait_for_timeout(700)
        alert_text = page.inner_text("#alerts")
        check("submission refused without a selection", "select exactly 1" in alert_text.lower(), alert_text)

        print("\n10. Selecting it, then submitting, locks the paper")
        page.check("#selectC")
        page.wait_for_timeout(600)
        page.on("dialog", lambda dialog: dialog.accept())
        page.click("#submitBtn")
        page.wait_for_url("**/submitted", timeout=15000)
        check("redirected to the confirmation", page.url.endswith("/submitted"), page.url)
        check("confirmation shown", "submission complete" in page.inner_text("body").lower())
        with SessionLocal() as db:
            attempt = db.scalar(select(Attempt))
            check("attempt locked", attempt.is_locked is True)
            check("PDF stored", attempt.pdf_bytes is not None and attempt.pdf_bytes[:4] == b"%PDF")
            check("section C answer kept", any(
                a.selected and a.question.section == "C" and "multi-home" in a.value
                for a in attempt.answers))

        print("\n11. No JavaScript errors across the whole sitting")
        check("page stayed clean", not errors, "; ".join(errors[:3]))

        browser.close()
    server.should_exit = True

    print(f"\n{checks - len(failures)} of {checks} checks passed")
    if failures:
        print("FAILURES:\n  - " + "\n  - ".join(failures))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
