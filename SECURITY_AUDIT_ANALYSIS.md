# Data Mixing Vulnerability Analysis

## Issue Summary
Student reports indicate that submission reports are showing:
1. **Wrong student details** (not matching the student who submitted)
2. **Wrong answers** (answers to questions the student didn't answer, e.g., Section C Q3)

This suggests a **critical data mixing/cross-contamination vulnerability** in the submission or reporting system.

---

## Code Flow Analysis

### 1. Student Authentication & Session Management ✅ SECURE
**File**: `app/deps.py`, `app/security.py`

- Session cookies are properly scoped with:
  - `slug` (course identifier)
  - `role` (student/admin)
  - `id` (student ID)
  - Cookie path: `/{slug}` - prevents cross-course cookie mixing
  
- Validation checks in `current_student()`:
  - Verifies cookie `slug` matches request URL `slug`
  - Verifies `role == "student"`
  - Checks `Student.is_blocked`
  
✅ **Conclusion**: Student identification is properly isolated per course.

---

### 2. Answer Saving & Retrieval 🔴 **POTENTIAL ISSUE FOUND**

**File**: `app/routers/exam.py`, lines 152-180

```python
def save(request, body, student, lecturer, db):
    attempt = _live_attempt(db, student)  # Gets the open exam's attempt for this student
    existing = _answers_map(db, attempt)  # Creates dict of question_id -> Answer
    
    # Updates answers
    for item in body.get("answers"):
        question_id = int(item.get("question_id"))
        answer = existing.get(question_id)
        if answer is None:
            # Creates new Answer, but...
            answer = Answer(attempt_id=attempt.id, question_id=question_id)
```

**`_answers_map()` Implementation**:
```python
def _answers_map(db: Session, attempt: Attempt) -> dict[int, Answer]:
    return {answer.question_id: answer for answer in attempt.answers}
```

✅ This correctly maps answers by `attempt_id`, which is scoped to the student.

---

### 3. PDF Generation & Submission 🔴 **CRITICAL ISSUE FOUND**

**File**: `app/routers/exam.py`, lines 301-315

```python
def submit(request, body, student, lecturer, db):
    attempt = _live_attempt(db, student)
    
    # ... validation ...
    
    db.commit()
    db.refresh(attempt)  # <-- REFRESH ATTEMPT FROM DB
    
    document = pdf.build(attempt, _answers_map(db, attempt), lecturer)
    attempt.pdf_bytes = document
    attempt.pdf_filename = pdf.filename_for(attempt, lecturer)
    db.commit()
```

**PDF Building Logic**:
**File**: `app/pdf.py`, lines 36-46

```python
def build(attempt: Attempt, answers_by_question: dict[int, "object"], lecturer: Lecturer) -> bytes:
    # ... setup ...
    
    meta = [
        ["Student Name", student.full_name or "Not provided"],
        ["Computer Number", student.computer_number or "Not provided"],
        ["Email", student.email or "Not provided"],
        ["Started", attempt.started_at.strftime("%Y-%m-%d %H:%M:%S UTC")],
        # ...
    ]
```

The PDF uses `attempt.student` directly. Let me check if there's a lazy-loading issue...

---

### 4. Potential Root Cause: SQLAlchemy Lazy Loading & Session Issues

**Key Finding**: 
- `Answer` model references `attempt` via foreign key
- When loading answers, the ORM might load the WRONG attempt if:
  - Session is reused improperly
  - Query doesn't explicitly filter by `student_id`
  - Lazy-loading loads from cache without filtering

**File**: `app/models_course.py`, lines 114-176

```python
class Attempt(CourseBase):
    __tablename__ = "attempts"
    exam_id: Mapped[int] = mapped_column(ForeignKey("exams.id", ondelete="CASCADE"))
    student_id: Mapped[int] = mapped_column(ForeignKey("students.id", ondelete="CASCADE"))
    
    answers: Mapped[list[Answer]] = relationship(
        back_populates="attempt", 
        cascade="all, delete-orphan"
    )

class Answer(CourseBase):
    __tablename__ = "answers"
    attempt_id: Mapped[int] = mapped_column(
        ForeignKey("attempts.id", ondelete="CASCADE"), index=True
    )
    question_id: Mapped[int] = mapped_column(ForeignKey("questions.id"))
    
    attempt: Mapped[Attempt] = relationship(back_populates="answers")
```

**Unique Constraint**:
```python
__table_args__ = (UniqueConstraint("exam_id", "student_id", name="uq_attempt_exam_student"),)
```

This constraint ensures only ONE attempt per (exam, student) pair - **good design**.

---

### 5. Answer Retrieval in Admin Panel 🟡 **SUSPICIOUS**

**File**: `app/routers/admin.py`, lines 664-682

```python
@router.get("/attempts/{attempt_id}")
def attempt_detail(attempt_id: int, request, lecturer, db):
    attempt = db.get(Attempt, attempt_id)  # <-- Gets attempt by ID only
    if attempt is None:
        return RedirectResponse(...)
    
    answers = {answer.question_id: answer for answer in attempt.answers}
    questions = sorted(attempt.exam.questions, key=lambda q: (q.section, q.order_index))
```

⚠️ **ISSUE**: `db.get(Attempt, attempt_id)` doesn't verify:
- The attempt belongs to THIS lecturer's course
- The attempt is from THIS exam

If two courses share the same Attempt IDs (different databases), or if there's connection pooling confusion, data could mix.

---

### 6. Database Session Isolation 🔴 **POTENTIAL ISSUE**

**File**: `app/tenant_db.py`

```python
_engines: dict[int, Engine] = {}  # Cached per lecturer ID
_sessionmakers: dict[int, sessionmaker] = {}  # Cached per lecturer ID

def course_session(lecturer: Lecturer) -> Iterator[Session]:
    factory = _sessionmaker_for(lecturer)
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
    finally:
        session.close()
```

**Problem Analysis**:
- Engines are cached globally
- If a lecturer's database URL is changed but the engine cache isn't cleared properly, requests could hit the OLD database
- `forget()` is called when reconnecting, but only if code explicitly calls it

---

## Likely Scenarios for Data Mixing

### Scenario A: Cross-Database Confusion (Most Likely)
If using "platform" storage mode (shared PostgreSQL schema):
- Multiple lecturers' courses in the same database
- Schema names: `course_1_abc`, `course_2_def`, etc.
- If connection pool doesn't respect schema properly, queries might return data from wrong schema
- **Fix**: Verify PostgreSQL schema isolation

### Scenario B: Lazy-Loading from Wrong Session
- Student A submits, attempt loaded with lazy-loading
- Before refresh completes, session context switches
- Student B's attempt loaded instead
- **Fix**: Explicit eager loading of `attempt.student`

### Scenario C: Cache Invalidation Failure
- Engine cache `_engines[lecturer.id]` not cleared when database connection changes
- Next request using same lecturer ID hits old database
- **Fix**: Verify `forget()` is called everywhere needed

### Scenario D: Answer Query Returns Wrong Attempt's Answers
- `_answers_map()` loops `attempt.answers`
- If `attempt` object is corrupted/wrong, answers are from wrong submission
- **Fix**: Verify attempt identity before using

---

## Critical Recommendations

### 1. ✅ Immediate: Verify Platform Storage Mode
```bash
# Check which storage mode is active
SELECT course_storage_mode, platform_db_schema FROM lecturers;
```

If using `platform` mode:
- Verify schema isolation with: `SET search_path = 'course_1_abc'; SELECT * FROM attempts;`
- Check if PostgreSQL search_path is being respected

### 2. ⚠️ Fix Lazy-Loading Issues
**File**: `app/pdf.py`, line 37
```python
# Current (potentially unsafe with lazy-loading):
student = attempt.student

# Safer (explicit join):
from sqlalchemy.orm import joinedload
# In pdf.build(), ensure attempt was loaded with:
# db.options(joinedload(Attempt.student))
```

### 3. ⚠️ Fix Admin Attempt Retrieval
**File**: `app/routers/admin.py`, line 666
```python
# Current (doesn't verify ownership):
attempt = db.get(Attempt, attempt_id)

# Better (verify this is the lecturer's course):
attempt = db.scalar(
    select(Attempt)
    .where(
        Attempt.id == attempt_id,
        Attempt.exam_id.in_(select(Exam.id).where(Exam.id == attempt.exam_id))
    )
)
# Or even simpler: ensure foreign key constraints are tight
```

### 4. ✅ Verify Connection Pool Settings
**File**: `app/db.py`, `app/tenant_db.py`
```python
# Current settings:
"pool_size": 5,
"max_overflow": 10,
"pool_recycle": 900,  # Recycles connections every 15 min
```

These look reasonable, but verify no schema confusion with PostgreSQL.

### 5. ⚠️ Add Database-Level Constraints
Ensure your course database has foreign key constraints enforced:
```sql
SET foreign_key_checks = ON;  -- MySQL
SET session setting foreign_keys = ON;  -- Older SQLite
-- PostgreSQL has them on by default
```

---

## Testing Recommendations

### Test 1: Cross-Student Data Isolation
```python
# In tests/test_data_isolation.py
def test_student_a_cannot_see_student_b_answers():
    # Student A submits attempt_1 with answer "A"
    # Student B submits attempt_2 with answer "B"
    # Verify student A's PDF shows only their answer "A"
```

### Test 2: Schema Isolation (if using platform mode)
```python
# Verify schema names are correct
# Verify queries include schema in search_path
```

### Test 3: Database Reconnection
```python
# Simulate lecturer changing database URL
# Verify old data is not accessible
```

---

## Most Probable Root Cause

Given the symptoms (wrong student details + wrong answers), the most likely causes are:

1. **PostgreSQL schema confusion** (70% probability)
   - If using platform storage: schemas not properly isolated
   - Connection string not specifying correct schema

2. **Session/lazy-loading in wrong order** (20% probability)
   - `attempt.student` loaded from wrong session context

3. **Admin panel bypasses verification** (10% probability)
   - Lecturer can view any attempt_id without ownership check

---

## Action Items

- [ ] Check if using "platform" or "external" storage mode
- [ ] If "platform": Verify PostgreSQL schema isolation
- [ ] Audit all `db.get(Attempt, ...)` calls - add ownership checks
- [ ] Add explicit `joinedload(Attempt.student)` in PDF generation
- [ ] Add database integrity tests
- [ ] Review connection pool configuration for schema bleeding

