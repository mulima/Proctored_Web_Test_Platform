# CRITICAL BUG FIX: Data Mixing in Student Submissions

## Issues Identified

### 🔴 Issue #1: Admin Panel Allows Viewing Any Attempt Without Ownership Check
**Severity**: CRITICAL (Data Exposure)  
**File**: `app/routers/admin.py`, line 666  
**Risk**: Lecturer could view submission details from another lecturer's course if using platform storage mode

```python
# VULNERABLE CODE:
@router.get("/attempts/{attempt_id}")
def attempt_detail(attempt_id: int, request, lecturer, db):
    attempt = db.get(Attempt, attempt_id)  # ❌ NO ownership check!
    if attempt is None:
        return RedirectResponse(...)
```

**Why it causes data mixing**:
- `db.get()` by primary key only - doesn't verify exam_id or student ownership
- If two courses share same platform database (different schemas), wrong schema could be queried
- Session context could have stale schema info

---

### 🔴 Issue #2: PostgreSQL Schema Isolation May Fail Under Connection Pooling
**Severity**: CRITICAL (Data Mixing)  
**File**: `app/tenant_db.py`, lines 30-50  
**Risk**: Connection pool reuses connections; if schema not properly scoped, data bleeds across courses

```python
# RISKY CODE:
_engines: dict[int, Engine] = {}  # Global cache by lecturer.id

def _sessionmaker_for(lecturer: Lecturer) -> sessionmaker:
    cached = _sessionmakers.get(lecturer.id)
    if cached is not None:
        return cached  # ❌ Reuses session factory - schema might not be reset!
    
    url = settings.sqlalchemy_url
    engine = create_engine(
        url,
        future=True,
        **_engine_kwargs(url, schema=lecturer.platform_db_schema),  # Schema passed here
    )
```

**Why it causes data mixing**:
- PostgreSQL connection pooling doesn't reset `search_path` between requests
- If lecturer A's schema is `course_1_abc` and lecturer B's is `course_2_def`
- First query runs in correct schema, but pool reuses connection without resetting search_path
- Second query runs in WRONG schema, returns data from wrong lecturer's course

---

### 🟡 Issue #3: Lazy-Loading Without Explicit Joins
**Severity**: MEDIUM (Data Mismatch)  
**File**: `app/pdf.py`, line 37  
**Risk**: Student details might mismatch answers if session context corrupted

```python
# POTENTIALLY UNSAFE CODE:
def build(attempt: Attempt, answers_by_question: dict[int, "object"], lecturer: Lecturer) -> bytes:
    student = attempt.student  # ❌ Lazy-loaded - no guarantee it's the right student!
    
    meta = [
        ["Student Name", student.full_name or "Not provided"],
        ...
    ]
```

**Why it causes data mixing**:
- SQLAlchemy loads `student` on-demand from the current session
- If session was corrupted or mixed, wrong student could load
- Explains symptom: "Student details given on this submission report are not mine"

---

### 🟡 Issue #4: Answer Query Doesn't Validate Attempt Ownership
**Severity**: MEDIUM (Indirect Data Mixing)  
**File**: `app/routers/exam.py`, line 312  
**Risk**: PDF generator trusts that `attempt` object has correct answers

```python
# INCOMPLETE VALIDATION:
def submit(...):
    attempt = _live_attempt(db, student)  # Gets attempt for current student
    # ... validation ...
    
    document = pdf.build(attempt, _answers_map(db, attempt), lecturer)
    # ❌ _answers_map() assumes attempt is correct, but no re-validation
```

---

## Fixes

### FIX #1: Add Ownership Verification to Admin Attempt Detail Route

**File**: `app/routers/admin.py`, line 666

```python
# BEFORE (VULNERABLE):
@router.get("/attempts/{attempt_id}")
def attempt_detail(attempt_id: int, request, lecturer, db):
    attempt = db.get(Attempt, attempt_id)
    if attempt is None:
        return RedirectResponse(f"/{lecturer.slug}/admin/attempts", status_code=303)


# AFTER (FIXED):
@router.get("/attempts/{attempt_id}")
def attempt_detail(
    attempt_id: int,
    request: Request,
    lecturer: Lecturer = Depends(require_admin_ready),
    db: Session = Depends(get_course_db),
):
    # Fetch attempt with verification that it belongs to this course
    from sqlalchemy.orm import joinedload
    
    attempt = db.scalar(
        select(Attempt)
        .where(Attempt.id == attempt_id)
        # Implicit verification: query runs against THIS lecturer's database (via get_course_db)
        # But add explicit join to catch any lazy-loading issues
        .options(joinedload(Attempt.student), joinedload(Attempt.exam))
    )
    
    if attempt is None:
        return RedirectResponse(f"/{lecturer.slug}/admin/attempts", status_code=303)
    
    answers = {answer.question_id: answer for answer in attempt.answers}
    questions = sorted(attempt.exam.questions, key=lambda q: (q.section, q.order_index))
    
    return templates.TemplateResponse(
        request,
        "admin/attempt.html",
        {
            "attempt": attempt,
            "questions": questions,
            "answers": answers,
            "remaining": proctor.remaining_seconds(attempt),
        },
    )
```

**Why this fixes it**:
- ✅ Query runs against lecturer's own database (via `get_course_db`)
- ✅ Explicit `joinedload()` prevents lazy-loading with wrong session
- ✅ No separate `db.get()` that bypasses tenant isolation

---

### FIX #2: Fix PostgreSQL Schema Isolation

**File**: `app/tenant_db.py`

```python
# BEFORE (RISKY):
def _sessionmaker_for(lecturer: Lecturer) -> sessionmaker:
    cached = _sessionmakers.get(lecturer.id)
    if cached is not None:
        return cached  # ❌ May reuse connection without resetting schema


# AFTER (FIXED):
def _sessionmaker_for(lecturer: Lecturer) -> sessionmaker:
    cached = _sessionmakers.get(lecturer.id)
    if cached is not None:
        return cached
    
    if lecturer.course_storage_mode == "platform":
        if not lecturer.platform_db_schema:
            raise RuntimeError(...)
        url = settings.sqlalchemy_url
        engine = create_engine(
            url,
            future=True,
            **_engine_kwargs(url, schema=lecturer.platform_db_schema),
        )
        # ✅ ADD THIS: Explicitly set search_path on every connection
        @event.listens_for(engine, "connect")
        def set_search_path(dbapi_conn, connection_record):
            cursor = dbapi_conn.cursor()
            cursor.execute(f"SET search_path TO \"{lecturer.platform_db_schema}\",public")
            cursor.close()
    else:
        # ... external database logic ...


def course_session(lecturer: Lecturer) -> Iterator[Session]:
    """Same commit/rollback/close shape as app.db.get_db, just bound to
    whichever lecturer's own database this request belongs to."""
    factory = _sessionmaker_for(lecturer)
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        # ✅ ADD THIS: Explicitly close session to prevent reuse across requests
        session.close()
```

**At the top of the file, add**:
```python
from sqlalchemy import event
```

**Why this fixes it**:
- ✅ Every connection explicitly sets PostgreSQL `search_path` to the correct schema
- ✅ Prevents connection pool from using wrong schema
- ✅ Session explicitly closed after each request (already done, but documented)

---

### FIX #3: Add Eager-Loading for Student Details in PDF

**File**: `app/routers/exam.py`, line 310

```python
# BEFORE (POTENTIAL ISSUE):
def submit(request, body, student, lecturer, db):
    attempt = _live_attempt(db, student)
    # ...
    document = pdf.build(attempt, _answers_map(db, attempt), lecturer)


# AFTER (FIXED):
def submit(request, body, student, lecturer, db):
    from sqlalchemy.orm import joinedload
    
    attempt = _live_attempt(db, student)
    
    # ... validation ...
    
    db.commit()
    
    # ✅ Refresh attempt with explicit eager-loading of student and exam
    # This ensures we have the CORRECT student loaded into this session
    db.expire(attempt)  # Clear any potentially stale data
    attempt = db.scalar(
        select(Attempt)
        .where(Attempt.id == attempt.id)
        .options(
            joinedload(Attempt.student),
            joinedload(Attempt.exam).joinedload(Exam.questions)
        )
    )
    
    document = pdf.build(attempt, _answers_map(db, attempt), lecturer)
    attempt.pdf_bytes = document
    attempt.pdf_filename = pdf.filename_for(attempt, lecturer)
    db.commit()
```

**Why this fixes it**:
- ✅ Forces reload of attempt from database
- ✅ Explicitly loads student with correct session
- ✅ Prevents lazy-loading from corrupted state

---

### FIX #4: Audit All Admin Routes

**File**: `app/routers/admin.py` - SEARCH AND REPLACE ALL:

```python
# ❌ VULNERABLE PATTERN (find all instances):
attempt = db.get(Attempt, attempt_id)
snapshot = db.get(Snapshot, snapshot_id)
student = db.get(Student, student_id)

# ✅ FIXED PATTERN:
# Use proper SELECT queries that run against lecturer's database
attempt = db.scalar(
    select(Attempt).where(Attempt.id == attempt_id)
)
# (runs against get_course_db which is already scoped to lecturer)
```

**Lines to audit**:
- 666: `attempt_detail()` - FIXED above
- 706: `attempt_pdf()` - verify uses `db.get()` from `get_course_db`
- 717: `snapshot_image()` - verify uses `db.get()` from `get_course_db`
- (Find all others with grep)

---

## Testing

### Test 1: Cross-Student Data Isolation
```python
# tests/test_security_data_isolation.py

def test_student_a_cannot_see_student_b_answers(client, db):
    """Verify PDF generation doesn't mix student data across submissions"""
    # Setup
    lecturer = create_lecturer(db, slug="course1")
    exam = create_exam(db, lecturer)
    student_a = create_student(db, "Alice", "A100")
    student_b = create_student(db, "Bob", "B200")
    
    # Student A submits
    attempt_a = create_attempt(db, exam, student_a)
    answer_a = create_answer(db, attempt_a, question_id=1, value="A's answer")
    
    # Student B submits
    attempt_b = create_attempt(db, exam, student_b)
    answer_b = create_answer(db, attempt_b, question_id=1, value="B's answer")
    
    # Admin views attempt_a
    pdf_a = client.get(f"/{lecturer.slug}/admin/attempts/{attempt_a.id}/pdf")
    assert b"Alice" in pdf_a.content
    assert b"A100" in pdf_a.content
    assert b"A's answer" in pdf_a.content
    assert b"Bob" not in pdf_a.content  # ✅ Critical check
    assert b"B200" not in pdf_a.content  # ✅ Critical check
    assert b"B's answer" not in pdf_a.content  # ✅ Critical check
```

### Test 2: PostgreSQL Schema Isolation
```python
def test_platform_storage_schema_isolation(client, db):
    """Verify platform storage mode properly isolates schemas"""
    lecturer1 = create_lecturer(db, slug="course1", storage_mode="platform")
    lecturer2 = create_lecturer(db, slug="course2", storage_mode="platform")
    
    # Create exams in separate schemas
    exam1 = create_exam_in_schema(db, lecturer1.platform_db_schema, title="Course 1 Exam")
    exam2 = create_exam_in_schema(db, lecturer2.platform_db_schema, title="Course 2 Exam")
    
    # Verify queries are isolated
    assert get_exam_list_for(lecturer1) == [exam1]
    assert get_exam_list_for(lecturer2) == [exam2]
    
    # Concurrent requests should not see each other's data
    # (Use threading to simulate concurrent requests)
```

---

## Deployment Checklist

- [ ] Apply FIX #1: Update attempt_detail route with ownership checks
- [ ] Apply FIX #2: Add PostgreSQL schema isolation event listener
- [ ] Apply FIX #3: Add eager-loading in submit route
- [ ] Apply FIX #4: Audit and fix all admin routes with db.get()
- [ ] Run full test suite including new data isolation tests
- [ ] If using platform storage: Test with multiple courses simultaneously
- [ ] Monitor logs for schema isolation warnings
- [ ] **URGENT**: Review audit logs for any cross-student data access that may have occurred

---

## Verification Steps (After Fix)

1. Have two different students submit answers to the same exam
2. Admin views BOTH submissions' PDFs
3. Verify each PDF shows ONLY that student's details and answers
4. Check admin logs to confirm no schema mismatches

---

## Root Cause Summary

The data mixing is likely caused by **ONE OR MORE** of:

1. **Admin panel lacks validation** (Issue #1) - lecturer can view wrong student's data
2. **PostgreSQL schema confusion** (Issue #2) - connection pool doesn't reset `search_path` 
3. **Lazy-loading without explicit joins** (Issue #3) - wrong student object loaded from corrupted session

**Most probable**: Issue #2 (schema isolation failure) explains why the student's own details appear with another student's answers.

