# ClearGrade Design

Assessment with clarity and integrity.

This document records the key design decisions behind ClearGrade so implementation, operations, and future changes stay aligned.

## 1. Product intent

ClearGrade is a shared, multi-tenant web platform for proctored course assessments.

Primary goals:
- Let many lecturers run independent courses from one deployment.
- Keep each course data boundary separate through lecturer-owned course databases.
- Preserve exam integrity through browser-side controls and durable evidence trails.
- Keep operational setup practical for low-resource university environments.

Non-goals:
- Full remote invigilation replacement.
- Perfect cheating detection from browser telemetry alone.
- Automatic schema management of each lecturer database.

## 2. High-level architecture

The system is split into two data planes:
- Platform plane: one deployment database for lecturer accounts, slug routing, and platform logs.
- Course plane: one database per lecturer course for students, exams, attempts, answers, incidents, snapshots, and course logs.

Routing model:
- Platform pages live at root paths such as /signup.
- Course pages are scoped by slug, for example /econ101/login and /econ101/admin.

Why this design:
- Shared deployment reduces hosting cost and admin overhead.
- Per-course databases limit cross-course blast radius and clarify ownership.
- Slug scoping keeps links human-friendly and operationally clear.

## 3. Data design

Platform schema responsibilities:
- Lecturer identity and verification status.
- Branding fields used as course-level UI identity.
- Encrypted course database credentials.
- Platform operational logs.

Course schema responsibilities:
- Student identity, verification, approval, and access status.
- Exam and question structures.
- Attempt lifecycle and lock state.
- Incident and snapshot evidence records.
- Course-scoped application logs.

Persistence policy:
- Alembic owns schema changes.
- The app does not call create_all.
- A course database must already contain expected tables before connection is accepted.

## 4. Authentication and account flows

Student flow:
- Register with name, email, computer number, password.
- Verify email by signed, time-limited link.
- Optional admin approval gate before sitting.
- Login is denied when blocked, unverified, or pending approval.

Lecturer flow:
- Platform signup and email verification.
- Login under course slug at /<slug>/admin/login.
- Complete setup by attaching and validating course database.

Session model:
- Signed cookie payload contains role, slug, and id.
- Cookie path is scoped to the course slug.
- Route dependencies reject slug mismatch to prevent cross-course cookie reuse.

## 5. Password reset design

Implemented student workflow:
- Entry point: /<slug>/forgot-password.
- If account exists, is verified, and not blocked, send reset email.
- Public response is always generic to prevent account enumeration.
- Reset form endpoint: /<slug>/reset-password.
- Reset token is signed, expiry-bound, slug-scoped, and tied to current password-hash marker.
- After a successful password change, previously issued tokens become unusable.

Security rationale:
- No reset token is stored in database state.
- Token invalidation occurs naturally on password change through marker mismatch.
- Expiry window is configurable by password_reset_token_max_age_seconds.

## 6. Proctoring model

Core principle:
- Continuous detection runs in the student browser.
- Server performs spot verification on uploaded incident snapshots.

Why:
- Continuous server-side inference does not scale economically per student stream.
- Browser-side processing avoids streaming webcam video to server.
- Snapshot re-check gives a second opinion for marking review.

Evidence behavior:
- Full-screen exits, hidden-tab events, camera failures, reconnects, and other incidents are logged with timestamps.
- Snapshot bytes and verdict metadata are stored durably with attempts.
- Incident logs are appended to submission artifacts for review.

## 7. Email subsystem

Backends:
- Console for development fallback.
- SMTP for common institutional and Gmail relay flows.
- Resend API for SMTP-restricted environments.

Resolution model:
- Platform defaults can be overridden per lecturer course.
- Sensitive lecturer email credentials are encrypted at rest.
- Mail failure should not abort critical exam flows such as submission.

## 8. Frontend and UX design

Brand:
- Product name: ClearGrade.
- Tagline: Assessment with clarity and integrity.

Landing design intent:
- Hero section with clear product identity and institutional visual anchor.
- Guided links for student registration, student login, and existing admin login using slug input.
- Mobile-first responsiveness across hero and dashboard layouts.

Dashboard design intent:
- Keep student actions simple and explicit during exam windows.
- Keep admin overview compact with clear counts and flagged-attempt visibility.

## 9. Deployment model

Target platform:
- Railway with Nixpacks.
- Startup command includes migration then app start.
- Health endpoint probes platform DB connectivity.

Operational requirements:
- Platform DATABASE_URL must be present and persistent.
- SECRET_KEY and CREDENTIAL_ENCRYPTION_KEY must be set explicitly.
- BASE_URL must match deployed hostname for email links.

## 10. Security and privacy decisions

Security decisions:
- Signed time-limited links for verification and reset flows.
- Password hashing via Argon2.
- Constant-time token comparison where appropriate.
- Course slug binding across sessions and token verification.

Privacy decisions:
- No continuous video upload to server.
- Snapshot capture is event-driven, not stream-driven.
- Explicit privacy page documents what is recorded.

## 11. Known limitations

- Browser constraints cannot replicate desktop-level process and window inspection.
- Phone detection is probabilistic and environment-sensitive.
- Course DB engine cache currently has no eviction policy for very high tenant counts.

## 12. Planned and deferred enhancements

High-priority backlog:
- Add password_changed_at for students and expose recent password-reset events in admin views.
- Add rate limiting for forgot-password and resend endpoints.
- Add stronger audit breadcrumbs around reset-link issuance and completion.

Medium-priority backlog:
- Add optional tenant engine cache eviction and telemetry for pool pressure.
- Add admin-facing operational status panel for email and model readiness.

Low-priority backlog:
- Expand branded theming controls per course while preserving accessibility constraints.

## 13. Related documents

- README.md for full system behavior and deployment context.
- docs/DEPLOYMENT.md for runbook steps.
- docs/DATABASE_SCHEMA.md and docs/DATABASE_SCHEMA.sql for course database contract.
