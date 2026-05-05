"""Generate a structured PDF export of a FAST run result.

Uses reportlab Platypus to build a multi-section PDF:
  - Cover / summary
  - Document metadata
  - Observations table (vision/benchmark mode) or findings table (classic mode)
  - Per-observation diff image (cropped to diff bbox when available)
"""
from __future__ import annotations

import io
import os
import textwrap
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    HRFlowable,
    Image as RLImage,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)


# ---------------------------------------------------------------------------
# Brand colours (FAST dark-theme palette translated to RGB 0-1)
# ---------------------------------------------------------------------------
_NAVY    = colors.HexColor("#0f1623")
_CARD    = colors.HexColor("#141c2d")
_CARD2   = colors.HexColor("#1a2236")
_TEXT    = colors.HexColor("#e2e8f0")
_MUTED   = colors.HexColor("#94a3b8")
_OK      = colors.HexColor("#4ade80")
_WARN    = colors.HexColor("#fbbf24")
_BAD     = colors.HexColor("#f87171")
_INFO    = colors.HexColor("#60a5fa")
_WHITE   = colors.white
_BLACK   = colors.black


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt_size(size_bytes: Optional[int]) -> str:
    if not size_bytes:
        return "—"
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.0f} KB"
    return f"{size_bytes / (1024 * 1024):.1f} MB"


def _fmt_date(dt: Any) -> str:
    if dt is None:
        return "—"
    try:
        return dt.strftime("%b %d, %Y %I:%M %p")
    except Exception:
        return str(dt)


def _page_count(instance_path: str, project_id: int, stored_filename: str) -> str:
    try:
        from pypdf import PdfReader
        path = os.path.join(instance_path, "uploads", f"project_{project_id}", "forms", stored_filename)
        if os.path.exists(path):
            return str(len(PdfReader(path).pages))
    except Exception:
        pass
    return "—"


def _crop_diff_image(
    snapshot_path: str,
    diff_bbox: Optional[list],
    instance_path: str,
    max_w_px: int = 1400,
) -> Optional[str]:
    """Return path to a (possibly cropped) version of the diff snapshot PNG.

    Saves cropped version to a temp file in instance/visual_diffs/ if needed.
    Returns the path string or None if the file doesn't exist.
    """
    try:
        from PIL import Image
        fname = os.path.basename(snapshot_path)
        vdir = Path(instance_path) / "visual_diffs"
        full_path = vdir / fname
        if not full_path.exists():
            return None

        img = Image.open(full_path).convert("RGB")
        iw, ih = img.size

        if diff_bbox and len(diff_bbox) == 4:
            _x0, y0, _x1, y1 = diff_bbox
            pad = 60
            cy0 = max(0, int(y0) - pad)
            cy1 = min(ih, int(y1) + pad)
            if (cy1 - cy0) < ih * 0.80:
                img = img.crop((0, cy0, iw, cy1))

        # Scale down if wider than max_w_px
        iw2, ih2 = img.size
        if iw2 > max_w_px:
            ratio = max_w_px / iw2
            img = img.resize((max_w_px, int(ih2 * ratio)), Image.LANCZOS)

        buf = io.BytesIO()
        img.save(buf, "PNG")
        buf.seek(0)
        return buf
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Styles
# ---------------------------------------------------------------------------

def _build_styles():
    base = getSampleStyleSheet()
    styles = {}

    styles["title"] = ParagraphStyle(
        "FASTTitle",
        parent=base["Title"],
        fontSize=22,
        textColor=_TEXT,
        backColor=_NAVY,
        spaceAfter=4 * mm,
        leading=28,
    )
    styles["h2"] = ParagraphStyle(
        "FASTH2",
        parent=base["Heading2"],
        fontSize=13,
        textColor=_INFO,
        spaceBefore=6 * mm,
        spaceAfter=2 * mm,
        leading=18,
    )
    styles["body"] = ParagraphStyle(
        "FASTBody",
        parent=base["Normal"],
        fontSize=10,
        textColor=_TEXT,
        leading=14,
        spaceAfter=2 * mm,
    )
    styles["muted"] = ParagraphStyle(
        "FASTMuted",
        parent=base["Normal"],
        fontSize=9,
        textColor=_MUTED,
        leading=13,
    )
    styles["obs"] = ParagraphStyle(
        "FASTObs",
        parent=base["Normal"],
        fontSize=9,
        textColor=_TEXT,
        leading=13,
        wordWrap="LTR",
    )
    styles["label"] = ParagraphStyle(
        "FASTLabel",
        parent=base["Normal"],
        fontSize=8,
        textColor=_MUTED,
        leading=11,
    )
    return styles


# ---------------------------------------------------------------------------
# Page template with header / footer
# ---------------------------------------------------------------------------

class _PageDecor:
    def __init__(self, project_name: str, tc_name: str):
        self.project = project_name
        self.tc = tc_name

    def __call__(self, canvas, doc):
        canvas.saveState()
        w, h = A4

        # Header bar
        canvas.setFillColor(_CARD)
        canvas.rect(0, h - 18 * mm, w, 18 * mm, fill=1, stroke=0)
        canvas.setFont("Helvetica-Bold", 9)
        canvas.setFillColor(_INFO)
        canvas.drawString(15 * mm, h - 11 * mm, "FAST  Validation Report")
        canvas.setFont("Helvetica", 9)
        canvas.setFillColor(_MUTED)
        canvas.drawRightString(w - 15 * mm, h - 11 * mm,
                               f"{self.project}  ·  {self.tc}")

        # Footer bar
        canvas.setFillColor(_CARD)
        canvas.rect(0, 0, w, 12 * mm, fill=1, stroke=0)
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(_MUTED)
        canvas.drawString(15 * mm, 4 * mm,
                          f"Generated {datetime.now().strftime('%b %d, %Y %I:%M %p')}")
        canvas.drawRightString(w - 15 * mm, 4 * mm,
                               f"Page {doc.page}")

        canvas.restoreState()


# ---------------------------------------------------------------------------
# Main builder
# ---------------------------------------------------------------------------

def generate_pdf_report(
    *,
    rr: Any,
    project: Any,
    tc: Any,
    main_form: Optional[Any],
    bench_form: Optional[Any],
    instance_path: str,
) -> bytes:
    import json as _json

    buf = io.BytesIO()
    page_w, page_h = A4

    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=15 * mm,
        rightMargin=15 * mm,
        topMargin=22 * mm,
        bottomMargin=16 * mm,
    )

    styles = _build_styles()
    project_name = getattr(project, "name", "") or "Project"
    tc_name = getattr(tc, "name", "") or "Test Case"

    result_obj: Dict[str, Any] = {}
    if getattr(rr, "result_json", None):
        try:
            result_obj = _json.loads(rr.result_json)
        except Exception:
            pass

    mode = (result_obj.get("mode") or getattr(tc, "mode", "") or "").lower()
    status = (getattr(rr, "status", "") or "").lower()
    observations: List[dict] = result_obj.get("observations") or []
    is_vision = mode == "benchmark" and bool(observations)

    # Verdict colour
    if status == "passed":
        verdict_color, verdict_label = _OK, "PASSED"
    elif status == "failed":
        verdict_color, verdict_label = _BAD, "FAILED"
    else:
        verdict_color, verdict_label = _WARN, "IN REVIEW"

    story = []

    # ── Cover section ────────────────────────────────────────────────────────
    story.append(Spacer(1, 8 * mm))
    story.append(Paragraph("FAST  Validation Report", styles["title"]))

    # Verdict chip
    story.append(
        Table(
            [[Paragraph(verdict_label, ParagraphStyle(
                "chip", fontSize=12, textColor=_WHITE, fontName="Helvetica-Bold"
            ))]],
            colWidths=[40 * mm],
            style=TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), verdict_color),
                ("ROUNDEDCORNERS", [6]),
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]),
        )
    )
    story.append(Spacer(1, 4 * mm))

    summary_text = (result_obj.get("overall_summary") or getattr(rr, "summary_text", "") or "").strip()
    if summary_text:
        story.append(Paragraph(summary_text, styles["body"]))

    story.append(HRFlowable(width="100%", thickness=1, color=_CARD2, spaceAfter=4 * mm))

    # Run metadata table
    run_rows = [
        ["Test Case", tc_name],
        ["Mode", mode.upper() or "—"],
        ["Run #", str(getattr(rr, "run_id", "—"))],
        ["Result #", str(getattr(rr, "id", "—"))],
        ["Status", verdict_label],
    ]
    meta_table = Table(
        run_rows,
        colWidths=[40 * mm, page_w - 30 * mm - 40 * mm],
        style=TableStyle([
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("TEXTCOLOR", (0, 0), (0, -1), _MUTED),
            ("TEXTCOLOR", (1, 0), (1, -1), _TEXT),
            ("FONTNAME", (0, 0), (0, -1), "Helvetica"),
            ("FONTNAME", (1, 0), (1, -1), "Helvetica-Bold"),
            ("TOPPADDING", (0, 0), (-1, -1), 2),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ("LINEBEFORE", (1, 0), (1, -1), 1, _CARD2),
            ("LEFTPADDING", (1, 0), (1, -1), 6),
        ]),
    )
    story.append(meta_table)

    # ── Document metadata ────────────────────────────────────────────────────
    story.append(Spacer(1, 6 * mm))
    story.append(Paragraph("Document Metadata", styles["h2"]))
    story.append(HRFlowable(width="100%", thickness=1, color=_CARD2, spaceAfter=3 * mm))

    def _form_row(label, form):
        if not form:
            return [label, "—", "—", "—", "—"]
        fname = getattr(form, "original_filename", "") or getattr(form, "stored_filename", "") or "—"
        sz = _fmt_size(getattr(form, "size_bytes", None))
        pgs = _page_count(instance_path, getattr(rr, "project_id", 0),
                          getattr(form, "stored_filename", "") or "")
        up = _fmt_date(getattr(form, "uploaded_at", None))
        return [label, fname, sz, pgs, up]

    doc_rows = [
        [Paragraph(h, styles["label"]) for h in
         ["", "Filename", "Size", "Pages", "Uploaded"]],
        _form_row("Main Form", main_form),
        _form_row("Benchmark Form", bench_form),
    ]
    doc_table = Table(
        doc_rows,
        colWidths=[30 * mm, 70 * mm, 20 * mm, 15 * mm, 45 * mm],
        style=TableStyle([
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("TEXTCOLOR", (0, 0), (-1, -1), _TEXT),
            ("TEXTCOLOR", (0, 0), (-1, 0), _MUTED),
            ("BACKGROUND", (0, 1), (-1, 1), _CARD),
            ("BACKGROUND", (0, 2), (-1, 2), _CARD2),
            ("ROWBACKGROUNDS", (0, 0), (-1, -1), [_CARD, _CARD2]),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ]),
    )
    story.append(doc_table)

    # ── Findings / Observations ──────────────────────────────────────────────
    story.append(Spacer(1, 6 * mm))
    if is_vision:
        story.append(Paragraph("Observations", styles["h2"]))
    else:
        story.append(Paragraph("Findings Summary", styles["h2"]))
    story.append(HRFlowable(width="100%", thickness=1, color=_CARD2, spaceAfter=3 * mm))

    if is_vision and observations:
        _CONF_COLOR = {"certain": _OK, "likely": _WARN, "possible": _INFO}
        visual = result_obj.get("visual_validation") or []
        snap_by_page: Dict[int, dict] = {}
        for v in visual:
            if isinstance(v, dict):
                sp = v.get("snapshot_path")
                if sp:
                    pg = v.get("actual_page_num")
                    if not pg:
                        try:
                            pg = int(str(v.get("page") or ""))
                        except Exception:
                            pass
                    if pg:
                        try:
                            snap_by_page[int(pg)] = {
                                "path": str(sp),
                                "bbox": v.get("diff_bbox"),
                            }
                        except Exception:
                            pass

        obs_rows = [[
            Paragraph(h, styles["label"]) for h in ["#", "Page", "Observation", "Confidence"]
        ]]
        for i, obs in enumerate(observations, 1):
            if not isinstance(obs, dict):
                continue
            conf = str(obs.get("confidence") or "possible").lower()
            obs_rows.append([
                Paragraph(str(i), styles["obs"]),
                Paragraph(str(obs.get("current_page") or "—"), styles["obs"]),
                Paragraph(textwrap.fill(str(obs.get("observation") or ""), 80), styles["obs"]),
                Paragraph(conf.upper(), ParagraphStyle(
                    "conf", fontSize=8, textColor=_CONF_COLOR.get(conf, _INFO),
                    fontName="Helvetica-Bold"
                )),
            ])

        obs_col_w = [10 * mm, 12 * mm, 120 * mm, 20 * mm]
        obs_table = Table(
            obs_rows,
            colWidths=obs_col_w,
            style=TableStyle([
                ("FONTSIZE", (0, 0), (-1, -1), 9),
                ("BACKGROUND", (0, 0), (-1, 0), _CARD2),
                ("TEXTCOLOR", (0, 0), (-1, 0), _MUTED),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [_CARD, _NAVY]),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("GRID", (0, 0), (-1, -1), 0.5, _CARD2),
            ]),
            repeatRows=1,
        )
        story.append(obs_table)

        # Per-observation diff images
        story.append(Spacer(1, 6 * mm))
        story.append(Paragraph("Diff Images", styles["h2"]))
        story.append(HRFlowable(width="100%", thickness=1, color=_CARD2, spaceAfter=3 * mm))

        usable_w = page_w - 30 * mm
        for i, obs in enumerate(observations, 1):
            if not isinstance(obs, dict):
                continue
            pg = None
            try:
                pg = int(str(obs.get("current_page") or ""))
            except Exception:
                pass
            snap_info = snap_by_page.get(pg) if pg else None
            if not snap_info:
                continue

            img_buf = _crop_diff_image(
                snap_info["path"], snap_info.get("bbox"), instance_path,
                max_w_px=int(usable_w * 3.78),  # points → ~96dpi pixels
            )
            if img_buf is None:
                continue

            story.append(
                Paragraph(
                    f"<b>#{i}</b>  Page {pg}  —  "
                    f"{textwrap.shorten(str(obs.get('observation') or ''), 120)}",
                    styles["muted"],
                )
            )
            try:
                rl_img = RLImage(img_buf, width=usable_w, kind="proportional")
                story.append(rl_img)
            except Exception:
                pass
            story.append(Spacer(1, 4 * mm))

    else:
        # Classic mode: show bucket summary
        BUCKETS = [
            ("value_mismatches", "Value Mismatches"),
            ("missing_content", "Missing Content"),
            ("extra_content", "Extra Content"),
            ("spelling_errors", "Spelling Errors"),
            ("format_issues", "Format Issues"),
            ("compliance_issues", "Compliance Issues"),
            ("visual_mismatches", "Visual Mismatches"),
            ("layout_anomalies", "Layout Anomalies"),
        ]
        all_rows = [[Paragraph(h, styles["label"]) for h in
                     ["Category", "Severity", "Page", "Description"]]]
        for bucket, label in BUCKETS:
            for item in result_obj.get(bucket) or []:
                if not isinstance(item, dict):
                    continue
                sev = str(item.get("severity") or "medium").upper()
                sev_color = {"CRITICAL": _BAD, "HIGH": _BAD, "MEDIUM": _WARN}.get(sev, _INFO)
                all_rows.append([
                    Paragraph(label, styles["obs"]),
                    Paragraph(sev, ParagraphStyle("sev", fontSize=8, textColor=sev_color,
                                                   fontName="Helvetica-Bold")),
                    Paragraph(str(item.get("page") or "—"), styles["obs"]),
                    Paragraph(textwrap.fill(str(item.get("description") or ""), 70), styles["obs"]),
                ])
        if len(all_rows) == 1:
            story.append(Paragraph("No findings recorded.", styles["muted"]))
        else:
            findings_table = Table(
                all_rows,
                colWidths=[35 * mm, 18 * mm, 12 * mm, 115 * mm],
                style=TableStyle([
                    ("BACKGROUND", (0, 0), (-1, 0), _CARD2),
                    ("TEXTCOLOR", (0, 0), (-1, 0), _MUTED),
                    ("ROWBACKGROUNDS", (0, 1), (-1, -1), [_CARD, _NAVY]),
                    ("FONTSIZE", (0, 0), (-1, -1), 9),
                    ("TOPPADDING", (0, 0), (-1, -1), 4),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                    ("LEFTPADDING", (0, 0), (-1, -1), 4),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("GRID", (0, 0), (-1, -1), 0.5, _CARD2),
                ]),
                repeatRows=1,
            )
            story.append(findings_table)

    # Build PDF
    doc.build(
        story,
        onFirstPage=_PageDecor(project_name, tc_name),
        onLaterPages=_PageDecor(project_name, tc_name),
    )
    buf.seek(0)
    return buf.read()
