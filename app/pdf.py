"""Submission PDF, carried over from the desktop build.

Same shape the markers already know: cover table, the three sections, then the
proctoring appendix. Built into memory rather than to disk, because the container
filesystem does not survive a deploy - the bytes go into the database and are mailed.
"""

import html
import io
import re
from datetime import datetime

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.pdfencrypt import StandardEncryption
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from app.config import settings
from app.models_course import Attempt
from app.models_platform import Lecturer


def clean_filename(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]+", "_", (value or "").strip())
    return value.strip("_") or "student"


def escape(value: str) -> str:
    return html.escape(value or "No answer").replace("\n", "<br/>")


def filename_for(attempt: Attempt, lecturer: Lecturer) -> str:
    stamp = (attempt.submitted_at or datetime.utcnow()).strftime("%Y%m%d_%H%M%S")
    return (
        f"{clean_filename(lecturer.file_prefix)}_{clean_filename(attempt.exam.title)}_"
        f"{clean_filename(attempt.student.computer_number)}_"
        f"{clean_filename(attempt.student.full_name)}_{stamp}.pdf"
    )


def build(attempt: Attempt, answers_by_question: dict[int, "object"], lecturer: Lecturer) -> bytes:
    styles = getSampleStyleSheet()
    title = ParagraphStyle(
        "DocTitle", parent=styles["Title"], fontName="Helvetica-Bold", fontSize=16, leading=20
    )
    heading = ParagraphStyle(
        "DocHeading", parent=styles["Heading2"], fontName="Helvetica-Bold", fontSize=12, leading=15
    )
    normal = ParagraphStyle("DocNormal", parent=styles["BodyText"], fontSize=9.5, leading=13)
    small = ParagraphStyle("DocSmall", parent=styles["BodyText"], fontSize=8.5, leading=11)

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        encrypt=StandardEncryption(
            "",
            ownerPassword=settings.secret_key[:32],
            canPrint=1,
            canModify=0,
            canCopy=0,
            canAnnotate=1,
            strength=128,
        ),
        rightMargin=18 * mm,
        leftMargin=18 * mm,
        topMargin=16 * mm,
        bottomMargin=16 * mm,
        title=f"{attempt.exam.title} Submission",
    )

    student = attempt.student
    strike_count = attempt.strike_count or 0
    summary = f"{strike_count} recorded incident(s)" if strike_count else "No incidents recorded"
    if attempt.flagged:
        summary += " - FLAGGED FOR REVIEW"

    story = [
        Paragraph(lecturer.footer.upper(), title),
        Paragraph(lecturer.subtitle, heading),
        Paragraph(f"{attempt.exam.title} - Student Submission", heading),
        Spacer(1, 6),
    ]

    meta = [
        ["Student Name", student.full_name or "Not provided"],
        ["Computer Number", student.computer_number or "Not provided"],
        ["Email", student.email or "Not provided"],
        ["Started", attempt.started_at.strftime("%Y-%m-%d %H:%M:%S UTC")],
        [
            "Submitted",
            (attempt.submitted_at or datetime.utcnow()).strftime("%Y-%m-%d %H:%M:%S UTC"),
        ],
        ["Submission Mode", attempt.submission_mode or "manual"],
        ["Proctoring", summary],
        ["PDF Status", "Editing restricted; marking annotations allowed"],
    ]
    table = Table(meta, colWidths=[38 * mm, 118 * mm])
    table.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#cfd6df")),
                ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#eef2f6")),
                ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("PADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )
    story.extend([table, Spacer(1, 12)])

    questions = sorted(attempt.exam.questions, key=lambda q: (q.section, q.order_index))
    section_a = [q for q in questions if q.section == "A"]
    section_b = [q for q in questions if q.section == "B"]
    section_c = [q for q in questions if q.section == "C"]

    if section_a:
        story.append(Paragraph("Section A: Multiple Choice", heading))
        rows = [["Q", "Answer"]]
        for index, question in enumerate(section_a, start=1):
            answer = answers_by_question.get(question.id)
            rows.append([str(index), (getattr(answer, "value", "") or "No answer")])
        mc_table = Table(rows, colWidths=[18 * mm, 130 * mm])
        mc_table.setStyle(
            TableStyle(
                [
                    ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#cfd6df")),
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#dfe7ef")),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("PADDING", (0, 0), (-1, -1), 4),
                ]
            )
        )
        story.extend([mc_table, PageBreak()])

    if section_b:
        story.append(Paragraph("Section B: Short Answer", heading))
        for index, question in enumerate(section_b, start=1):
            answer = answers_by_question.get(question.id)
            story.append(Paragraph(f"Question {index}", heading))
            story.append(Paragraph(escape(question.prompt), small))
            story.append(Spacer(1, 3))
            story.append(Paragraph(escape(getattr(answer, "value", "")), normal))
            story.append(Spacer(1, 9))
        story.append(PageBreak())

    if section_c:
        story.append(Paragraph("Section C: Long Writeup", heading))
        submitted_any = False
        for question in section_c:
            answer = answers_by_question.get(question.id)
            if not getattr(answer, "selected", False):
                continue
            submitted_any = True
            story.append(Paragraph(escape(question.title or question.prompt[:80]), heading))
            story.append(Paragraph(escape(question.prompt), small))
            story.append(Spacer(1, 4))
            story.append(Paragraph(escape(getattr(answer, "value", "")), normal))
            story.append(Spacer(1, 12))
        if not submitted_any:
            story.append(
                Paragraph("No Section C questions were selected for marking.", small)
            )

    story.extend(_incident_appendix(attempt, heading, small))
    doc.build(story)
    return buffer.getvalue()


def _incident_appendix(attempt: Attempt, heading, small) -> list:
    flow = [PageBreak(), Paragraph("Appendix: Proctoring Incident Log", heading)]
    incidents = list(attempt.incidents)
    if not incidents:
        flow.append(
            Paragraph("No proctoring incidents were recorded during this sitting.", small)
        )
        return flow

    counted = sum(1 for incident in incidents if incident.counted)
    verdict = (
        "This paper is FLAGGED FOR REVIEW. A flag is a prompt to look, not a finding of "
        "misconduct - webcams misread ordinary movement, and the log below should be read "
        "alongside the invigilator's own record."
        if attempt.flagged
        else "This paper is not flagged."
    )
    snapshot_count = len(attempt.snapshots)
    evidence = (
        f" {snapshot_count} evidence snapshot(s) were captured and are available in the "
        "admin panel."
        if snapshot_count
        else ""
    )
    flow.append(
        Paragraph(
            f"{counted} incident(s) counted towards the strike total; {len(incidents)} "
            f"event(s) recorded in all. {verdict}{evidence}",
            small,
        )
    )
    flow.append(Spacer(1, 6))

    header_style = ParagraphStyle(
        "IncidentHeader", parent=small, fontName="Helvetica-Bold", leading=10
    )
    rows = [
        [
            Paragraph(text, header_style)
            for text in ("Time (UTC)", "Into test", "Incident", "Counted", "Detail")
        ]
    ]
    for incident in incidents:
        minutes, seconds = divmod(int(incident.elapsed_seconds or 0), 60)
        rows.append(
            [
                Paragraph(incident.occurred_at.strftime("%Y-%m-%d %H:%M:%S"), small),
                Paragraph(f"{minutes:02d}:{seconds:02d}", small),
                Paragraph(html.escape(incident.label or incident.category), small),
                Paragraph("Yes" if incident.counted else "No", small),
                Paragraph(escape(str(incident.detail or "")[:300]), small),
            ]
        )
    table = Table(rows, colWidths=[28 * mm, 16 * mm, 44 * mm, 17 * mm, 69 * mm], repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#cfd6df")),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#dfe7ef")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("PADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    flow.append(table)
    return flow
