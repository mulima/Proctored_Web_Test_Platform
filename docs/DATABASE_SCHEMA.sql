BEGIN;

CREATE TABLE app_logs (
    id SERIAL NOT NULL, 
    at TIMESTAMP WITHOUT TIME ZONE DEFAULT (CURRENT_TIMESTAMP) NOT NULL, 
    level VARCHAR(10) NOT NULL, 
    event_type VARCHAR(60) NOT NULL, 
    message TEXT NOT NULL, 
    student_id INTEGER, 
    attempt_id INTEGER, 
    ip VARCHAR(64) NOT NULL, 
    user_agent VARCHAR(400) NOT NULL, 
    payload JSON, 
    PRIMARY KEY (id)
);

CREATE INDEX ix_app_logs_at ON app_logs (at);

CREATE INDEX ix_app_logs_attempt_id ON app_logs (attempt_id);

CREATE INDEX ix_app_logs_event_type ON app_logs (event_type);

CREATE INDEX ix_app_logs_level ON app_logs (level);

CREATE INDEX ix_app_logs_student_id ON app_logs (student_id);

CREATE TABLE exams (
    id SERIAL NOT NULL, 
    code VARCHAR(50) NOT NULL, 
    title VARCHAR(200) NOT NULL, 
    instructions TEXT NOT NULL, 
    duration_minutes INTEGER NOT NULL, 
    total_marks INTEGER NOT NULL, 
    section_c_required INTEGER NOT NULL, 
    is_open BOOLEAN NOT NULL, 
    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT (CURRENT_TIMESTAMP) NOT NULL, 
    PRIMARY KEY (id)
);

CREATE TABLE students (
    id SERIAL NOT NULL, 
    full_name VARCHAR(200) NOT NULL, 
    email VARCHAR(255) NOT NULL, 
    computer_number VARCHAR(50) NOT NULL, 
    password_hash VARCHAR(255) NOT NULL, 
    is_verified BOOLEAN NOT NULL, 
    verified_at TIMESTAMP WITHOUT TIME ZONE, 
    verification_sent_at TIMESTAMP WITHOUT TIME ZONE, 
    verification_token VARCHAR(255), 
    is_approved BOOLEAN NOT NULL, 
    is_blocked BOOLEAN NOT NULL, 
    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT (CURRENT_TIMESTAMP) NOT NULL, 
    last_login_at TIMESTAMP WITHOUT TIME ZONE, 
    PRIMARY KEY (id)
);

CREATE UNIQUE INDEX ix_students_computer_number ON students (computer_number);

CREATE UNIQUE INDEX ix_students_email ON students (email);

CREATE INDEX ix_students_verification_token ON students (verification_token);

CREATE TABLE attempts (
    id SERIAL NOT NULL, 
    exam_id INTEGER NOT NULL, 
    student_id INTEGER NOT NULL, 
    started_at TIMESTAMP WITHOUT TIME ZONE DEFAULT (CURRENT_TIMESTAMP) NOT NULL, 
    deadline_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
    elapsed_high_water_seconds FLOAT NOT NULL, 
    last_seen_at TIMESTAMP WITHOUT TIME ZONE, 
    current_question INTEGER NOT NULL, 
    submitted_at TIMESTAMP WITHOUT TIME ZONE, 
    is_locked BOOLEAN NOT NULL, 
    submission_mode VARCHAR(20) NOT NULL, 
    auto_submit_reason TEXT NOT NULL, 
    strike_count INTEGER NOT NULL, 
    flagged BOOLEAN NOT NULL, 
    last_alert_email_at TIMESTAMP WITHOUT TIME ZONE, 
    pdf_filename VARCHAR(300) NOT NULL, 
    pdf_bytes BYTEA, 
    PRIMARY KEY (id), 
    FOREIGN KEY(exam_id) REFERENCES exams (id), 
    FOREIGN KEY(student_id) REFERENCES students (id), 
    CONSTRAINT uq_attempt_exam_student UNIQUE (exam_id, student_id)
);

CREATE INDEX ix_attempts_exam_id ON attempts (exam_id);

CREATE INDEX ix_attempts_student_id ON attempts (student_id);

CREATE TABLE questions (
    id SERIAL NOT NULL, 
    exam_id INTEGER NOT NULL, 
    section VARCHAR(1) NOT NULL, 
    order_index INTEGER NOT NULL, 
    title VARCHAR(300) NOT NULL, 
    prompt TEXT NOT NULL, 
    options JSON, 
    marks INTEGER NOT NULL, 
    PRIMARY KEY (id), 
    FOREIGN KEY(exam_id) REFERENCES exams (id) ON DELETE CASCADE
);

CREATE INDEX ix_questions_exam_id ON questions (exam_id);

CREATE INDEX ix_questions_exam_section_order ON questions (exam_id, section, order_index);

CREATE TABLE answers (
    id SERIAL NOT NULL, 
    attempt_id INTEGER NOT NULL, 
    question_id INTEGER NOT NULL, 
    value TEXT NOT NULL, 
    selected BOOLEAN NOT NULL, 
    updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT (CURRENT_TIMESTAMP) NOT NULL, 
    PRIMARY KEY (id), 
    FOREIGN KEY(attempt_id) REFERENCES attempts (id) ON DELETE CASCADE, 
    FOREIGN KEY(question_id) REFERENCES questions (id), 
    CONSTRAINT uq_answer_attempt_question UNIQUE (attempt_id, question_id)
);

CREATE INDEX ix_answers_attempt_id ON answers (attempt_id);

CREATE INDEX ix_answers_question_id ON answers (question_id);

CREATE TABLE incidents (
    id SERIAL NOT NULL, 
    attempt_id INTEGER NOT NULL, 
    category VARCHAR(40) NOT NULL, 
    label VARCHAR(120) NOT NULL, 
    detail TEXT NOT NULL, 
    source VARCHAR(20) NOT NULL, 
    counted BOOLEAN NOT NULL, 
    occurred_at TIMESTAMP WITHOUT TIME ZONE DEFAULT (CURRENT_TIMESTAMP) NOT NULL, 
    elapsed_seconds INTEGER NOT NULL, 
    PRIMARY KEY (id), 
    FOREIGN KEY(attempt_id) REFERENCES attempts (id) ON DELETE CASCADE
);

CREATE INDEX ix_incidents_attempt_id ON incidents (attempt_id);

CREATE INDEX ix_incidents_category ON incidents (category);

CREATE TABLE snapshots (
    id SERIAL NOT NULL, 
    attempt_id INTEGER NOT NULL, 
    incident_id INTEGER, 
    image BYTEA NOT NULL, 
    mime VARCHAR(40) NOT NULL, 
    byte_size INTEGER NOT NULL, 
    captured_at TIMESTAMP WITHOUT TIME ZONE DEFAULT (CURRENT_TIMESTAMP) NOT NULL, 
    server_verdict VARCHAR(30) NOT NULL, 
    server_confidence FLOAT NOT NULL, 
    PRIMARY KEY (id), 
    FOREIGN KEY(attempt_id) REFERENCES attempts (id) ON DELETE CASCADE, 
    FOREIGN KEY(incident_id) REFERENCES incidents (id) ON DELETE SET NULL
);

CREATE INDEX ix_snapshots_attempt_id ON snapshots (attempt_id);

CREATE INDEX ix_snapshots_incident_id ON snapshots (incident_id);

COMMIT;

