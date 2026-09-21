"""PDF report generation (the "query -> render -> store-and-link" step).

Pipelines the whole workshop in one feature:

  1. Query   - task data comes through the repository's SQL aggregation
               (`aggregate_tasks`) plus the raw task rows, and (when present)
               the Week 4 books catalogue from books.json.
  2. Render  - the summary + tables are drawn into a PDF with ReportLab.
  3. Store   - the finished PDF is written straight to disk in the artifacts
               directory. The job never passes the bytes around: it only
               records the artifact *name* + *size*, and the API links to it
               via GET /reports/{id}/download.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

REPORT_KIND = "task_report"
MAX_TASKS_IN_REPORT = 200

_INK = colors.HexColor("#0f172a")
_MUTED = colors.HexColor("#64748b")
_EMERALD = colors.HexColor("#059669")
_AMBER = colors.HexColor("#d97706")
_ZEBRA = colors.HexColor("#f1f5f9")
_GRID = colors.HexColor("#cbd5e1")


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def aggregate_books(books_path: str | Path | None) -> dict | None:
    """Summarise the books.json catalogue; None when absent or unreadable."""
    if books_path is None:
        return None
    path = Path(books_path)
    if not path.is_file():
        return None
    try:
        books = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(books, list) or not books:
        return None

    priced = [b for b in books if isinstance(b.get("price"), (int, float))]
    total = sum(float(b["price"]) for b in priced) if priced else 0.0
    most_expensive = max(priced, key=lambda b: float(b["price"])) if priced else None
    best_rated = sorted(
        books,
        key=lambda b: (b.get("rating") or 0, b.get("price") or 0),
        reverse=True,
    )[:3]

    return {
        "count": len(books),
        "avg_price": round(total / len(priced), 2) if priced else None,
        "currency": priced[0].get("currency", "GBP") if priced else "GBP",
        "most_expensive": most_expensive["title"] if most_expensive else None,
        "best_rated": [b.get("title") for b in best_rated] or None,
    }


def _styles():
    sheet = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "ReportTitle",
            parent=sheet["Title"],
            fontSize=21,
            leading=25,
            textColor=_INK,
            spaceAfter=2,
        ),
        "meta": ParagraphStyle(
            "Meta",
            parent=sheet["Normal"],
            fontSize=9,
            leading=12,
            textColor=_MUTED,
        ),
        "heading": ParagraphStyle(
            "SectionHeading",
            parent=sheet["Heading2"],
            fontSize=12,
            leading=15,
            textColor=_INK,
            spaceBefore=12,
            spaceAfter=4,
        ),
        "cell": ParagraphStyle(
            "Cell",
            parent=sheet["Normal"],
            fontSize=8.5,
            leading=11,
            textColor=_INK,
        ),
    }


def _metric_table(rows, widths) -> Table:
    table = Table(rows, colWidths=widths)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), _INK),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTNAME", (0, 1), (0, -1), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 8.5),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, _ZEBRA]),
                ("GRID", (0, 0), (-1, -1), 0.4, _GRID),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]
        )
    )
    return table


def _summary_table(st, aggregate: dict):
    rows = [
        ["Metric", "Value"],
        ["Total tasks", str(aggregate["total"])],
        ["Completed", str(aggregate["done"])],
        ["Open", str(aggregate["open"])],
        ["Completion rate", f"{aggregate['completion_rate']:.1f}%"],
    ]
    return _metric_table(rows, [80 * mm, 45 * mm])


def _tasks_table(tasks: list[dict]) -> Table:
    rows = [["ID", "Title", "Status"]]
    shown = tasks[:MAX_TASKS_IN_REPORT]
    for task in shown:
        rows.append(
            [
                str(task["id"]),
                task["title"],
                "Done" if bool(task.get("done")) else "Open",
            ]
        )
    if len(tasks) > MAX_TASKS_IN_REPORT:
        rows.append(["…", f"… and {len(tasks) - MAX_TASKS_IN_REPORT} more", ""])

    table = Table(rows, colWidths=[16 * mm, 88 * mm, 22 * mm], repeatRows=1)
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), _INK),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8.5),
        ("GRID", (0, 0), (-1, -1), 0.4, _GRID),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("ROWBACKGROUNDS", (0, 2), (-1, -1), [colors.white, _ZEBRA]),
    ]
    for i in range(1, len(rows)):
        status = rows[i][2]
        if status == "Done":
            style.append(("TEXTCOLOR", (2, i), (2, i), _EMERALD))
        elif status == "Open":
            style.append(("TEXTCOLOR", (2, i), (2, i), _AMBER))
    table.setStyle(TableStyle(style))
    return table


def _books_table(books: dict) -> Table:
    avg = "—" if books["avg_price"] is None else f"{books['avg_price']:.2f} {books['currency']}"
    rows = [
        ["Metric", "Value"],
        ["Books catalogued", str(books["count"])],
        ["Average price", avg],
        ["Most expensive", books["most_expensive"] or "—"],
        ["Top rated", "; ".join(books["best_rated"] or [])],
    ]
    return _metric_table(rows, [44 * mm, 81 * mm])


def build_task_report_pdf(
    destination,
    tasks: list[dict],
    aggregate: dict,
    books: dict | None = None,
    source_name: str = "database",
    generated_at: str | None = None,
) -> None:
    """Render the task activity report and write it to `destination`."""
    generated_at = generated_at or iso_now()
    st = _styles()
    doc = SimpleDocTemplate(
        str(destination),
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=16 * mm,
        bottomMargin=16 * mm,
        title="Task Activity Report",
        author="PulseAPI",
    )

    story = [
        Paragraph("Task Activity Report", st["title"]),
        Paragraph(f"Generated {generated_at} · Data source: {source_name}", st["meta"]),
        Spacer(1, 4 * mm),
        Paragraph("Summary", st["heading"]),
        _summary_table(st, aggregate),
        Spacer(1, 4 * mm),
        Paragraph("Tasks", st["heading"]),
        _tasks_table(tasks),
    ]

    if books:
        story += [
            Spacer(1, 4 * mm),
            Paragraph("Books catalogue", st["heading"]),
            _books_table(books),
        ]

    doc.build(story)


def run_task_report_job(repo, artifact_dir, books_path, job_id: str) -> dict:
    """Execute the "task_report" job: query, render to disk, return artifact meta."""
    artifact_dir = Path(artifact_dir)
    artifact_dir.mkdir(parents=True, exist_ok=True)

    tasks = repo.list_tasks()
    aggregate = repo.aggregate_tasks()
    books = aggregate_books(books_path)
    source_name = "PostgreSQL" if "Postgres" in type(repo).__name__ else "SQLite"

    destination = artifact_dir / f"report-{job_id}.pdf"
    build_task_report_pdf(
        destination,
        tasks=tasks,
        aggregate=aggregate,
        books=books,
        source_name=source_name,
    )

    return {
        "artifact_name": destination.name,
        "artifact_bytes": destination.stat().st_size,
    }