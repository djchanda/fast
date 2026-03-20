"""
PDF Annotator — overlays finding annotations directly onto PDF pages.
Produces an annotated copy of the PDF with color-coded callouts.

Requires: pypdf >= 5.0 and reportlab
"""
from __future__ import annotations

import io
import json
from typing import Any, Dict, List, Optional


SEVERITY_COLORS = {
    "critical": (1.0, 0.0, 0.0),    # red
    "high": (1.0, 0.4, 0.0),        # orange
    "medium": (1.0, 0.8, 0.0),      # yellow
    "low": (0.0, 0.6, 0.0),         # green
    "warning": (1.0, 0.6, 0.0),     # amber
}


def annotate_pdf(pdf_bytes: bytes, result_json: Dict[str, Any]) -> bytes:
    """
    Overlay finding annotations on the PDF.

    Args:
        pdf_bytes: Raw bytes of the original PDF.
        result_json: Validation findings dict (from runner output).

    Returns:
        Annotated PDF as bytes. Falls back to original bytes if reportlab not available.
    """
    try:
        from reportlab.lib.colors import Color
        from reportlab.pdfgen import canvas as rl_canvas
        from pypdf import PdfReader, PdfWriter
    except ImportError:
        # If reportlab is not installed, return original unchanged
        return pdf_bytes

    # Collect page → list of (severity, label, description)
    page_findings: Dict[int, List[Dict]] = {}

    categories = [
        "spelling_errors", "format_issues", "value_mismatches",
        "missing_content", "extra_content", "layout_anomalies",
        "compliance_issues", "visual_mismatches",
    ]
    for cat in categories:
        for item in result_json.get(cat, []):
            page = int(item.get("page", 0))
            if page < 1:
                page = 1
            page_findings.setdefault(page, []).append({
                "severity": item.get("severity", "low"),
                "label": cat.replace("_", " ").title(),
                "description": item.get("description", ""),
            })

    if not page_findings:
        return pdf_bytes

    reader = PdfReader(io.BytesIO(pdf_bytes))
    writer = PdfWriter()

    for page_idx, page in enumerate(reader.pages):
        page_num = page_idx + 1
        findings = page_findings.get(page_num, [])

        if findings:
            # Build an overlay canvas
            media_box = page.mediabox
            width = float(media_box.width)
            height = float(media_box.height)

            overlay_buf = io.BytesIO()
            c = rl_canvas.Canvas(overlay_buf, pagesize=(width, height))

            y_offset = height - 20  # start near top

            for idx, f in enumerate(findings[:10]):  # max 10 per page
                severity = f["severity"].lower()
                r, g, b = SEVERITY_COLORS.get(severity, (0.5, 0.5, 0.5))
                color = Color(r, g, b, alpha=0.85)

                # Draw a colored banner
                c.setFillColor(color)
                c.setStrokeColorRGB(0, 0, 0)
                c.roundRect(5, y_offset - 14, width - 10, 16, 3, fill=1, stroke=0)

                # Draw text
                c.setFillColorRGB(1, 1, 1)
                c.setFont("Helvetica-Bold", 7)
                label = f"[{severity.upper()}] {f['label']}: {f['description'][:80]}"
                c.drawString(10, y_offset - 11, label[:110])

                y_offset -= 18
                if y_offset < 30:
                    break  # no more room

            c.save()
            overlay_buf.seek(0)

            overlay_reader = PdfReader(overlay_buf)
            overlay_page = overlay_reader.pages[0]
            page.merge_page(overlay_page)

        writer.add_page(page)

    out_buf = io.BytesIO()
    writer.write(out_buf)
    out_buf.seek(0)
    return out_buf.read()
