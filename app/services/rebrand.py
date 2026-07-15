"""
Bulk Form Rebranding Service.

Stamps new branding (logo, company name, colour header, footer) onto every
page of selected PDFs using the same reportlab + pypdf overlay technique as
engine/pdf_annotator.py.  No new libraries needed.
"""
from __future__ import annotations

import io
import os
import zipfile
from datetime import datetime
from typing import List

import logging

logger = logging.getLogger(__name__)


def _hex_to_rgb(hex_color: str):
    """Convert '#003087' → (0.0, 0.188, 0.529)."""
    h = (hex_color or "#000000").lstrip("#")
    if len(h) != 6:
        h = "000000"
    return tuple(int(h[i:i+2], 16) / 255.0 for i in (0, 2, 4))


def apply_branding_to_pdf(pdf_bytes: bytes, profile) -> bytes:
    """
    Overlay branding stamp on every page of *pdf_bytes*.

    Stamp layers (bottom → top on each page):
      1. White rectangle covering the top header_height points (hides old header)
      2. Coloured rectangle (primary_color) as new header bar
      3. Logo image in top-left of header (if logo_path set and exists)
      4. Company name text centred in header
      5. Thin coloured footer strip with footer_text (if set)

    Returns rebranded PDF as bytes.
    """
    try:
        from reportlab.pdfgen import canvas as rl_canvas
        from pypdf import PdfReader, PdfWriter
    except ImportError as exc:
        logger.error("rebrand: missing library — %s", exc)
        return pdf_bytes

    hdr = max(int(profile.header_height or 60), 20)
    r, g, b = _hex_to_rgb(profile.primary_color)

    reader = PdfReader(io.BytesIO(pdf_bytes))
    writer = PdfWriter()

    for page in reader.pages:
        w = float(page.mediabox.width)
        h = float(page.mediabox.height)

        # ── Build the stamp canvas ──────────────────────────────
        buf = io.BytesIO()
        c = rl_canvas.Canvas(buf, pagesize=(w, h))

        # 1. White block — covers old header completely
        c.setFillColorRGB(1, 1, 1)
        c.rect(0, h - hdr, w, hdr, fill=1, stroke=0)

        # 2. Coloured header bar
        c.setFillColorRGB(r, g, b)
        c.rect(0, h - hdr, w, hdr, fill=1, stroke=0)

        # 3. Logo image
        logo_drawn_width = 0
        if profile.logo_path and os.path.exists(profile.logo_path):
            try:
                logo_h = hdr - 12
                logo_w = min(logo_h * 4, w * 0.25)   # at most 25% of page width
                c.drawImage(
                    profile.logo_path,
                    10,
                    h - hdr + 6,
                    width=logo_w,
                    height=logo_h,
                    preserveAspectRatio=True,
                    mask="auto",
                )
                logo_drawn_width = logo_w + 10
            except Exception as exc:
                logger.warning("rebrand: logo draw failed — %s", exc)

        # 4. Company name
        if profile.company_name:
            font_size = min(14, max(8, int(hdr * 0.28)))
            c.setFillColorRGB(1, 1, 1)
            c.setFont("Helvetica-Bold", font_size)
            # Centre in the space to the right of the logo
            text_x = logo_drawn_width + 10 + (w - logo_drawn_width - 20) / 2 - len(profile.company_name) * font_size * 0.3
            text_y = h - hdr / 2 - font_size / 2
            c.drawString(max(logo_drawn_width + 10, text_x), text_y, profile.company_name)

        # Tagline (smaller, below company name)
        if profile.tagline:
            tag_size = max(6, font_size - 4 if profile.company_name else 8)
            c.setFillColorRGB(1, 1, 1, 0.75)
            c.setFont("Helvetica", tag_size)
            tag_x = logo_drawn_width + 10 + (w - logo_drawn_width - 20) / 2 - len(profile.tagline) * tag_size * 0.25
            c.drawString(max(logo_drawn_width + 10, tag_x), text_y - tag_size - 2, profile.tagline)

        # 5. Footer strip
        if profile.footer_text:
            footer_h = 18
            c.setFillColorRGB(r, g, b)
            c.rect(0, 0, w, footer_h, fill=1, stroke=0)
            c.setFillColorRGB(1, 1, 1)
            c.setFont("Helvetica", 7)
            c.drawCentredString(w / 2, 5, profile.footer_text)

        c.save()
        buf.seek(0)

        # ── Merge stamp over the original page ──────────────────
        stamp_page = PdfReader(buf).pages[0]
        page.merge_page(stamp_page, over=True)
        writer.add_page(page)

    out = io.BytesIO()
    writer.write(out)
    out.seek(0)
    return out.read()


def bulk_rebrand(form_ids: List[int], profile, project_id: int, instance_path: str) -> dict:
    """
    Apply branding to each Form in *form_ids*.

    Saves:
    - Rebranded PDF file on disk next to the original
    - New Form DB record (version='rebranded')
    - All rebranded PDFs in an in-memory ZIP

    Returns:
        {
            "saved_form_ids": [int, ...],
            "zip_bytes": bytes,
            "errors": [str, ...],
        }
    """
    from app.extensions import db
    from app.models.form import Form

    saved_ids: list = []
    errors: list = []
    zip_buf = io.BytesIO()

    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for fid in form_ids:
            form = Form.query.filter_by(id=fid, project_id=project_id).first()
            if not form:
                errors.append(f"Form #{fid} not found")
                continue

            abs_path = os.path.join(instance_path, form.file_path) if not os.path.isabs(form.file_path) else form.file_path

            if not os.path.exists(abs_path):
                errors.append(f"File not found for Form #{fid}: {form.file_path}")
                continue

            try:
                with open(abs_path, "rb") as fp:
                    original_bytes = fp.read()

                rebranded_bytes = apply_branding_to_pdf(original_bytes, profile)

                # ── Persist rebranded file ──────────────────────
                base_name = os.path.splitext(os.path.basename(abs_path))[0]
                new_filename = f"{base_name}_rebranded_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}.pdf"
                form_dir = os.path.dirname(abs_path)
                new_abs_path = os.path.join(form_dir, new_filename)

                with open(new_abs_path, "wb") as fp:
                    fp.write(rebranded_bytes)

                # ── Relative path for DB ────────────────────────
                rel_path = os.path.relpath(new_abs_path, instance_path)

                new_form = Form(
                    project_id=project_id,
                    name=f"{form.name} [Rebranded]",
                    file_path=rel_path,
                    original_filename=new_filename,
                    stored_filename=new_filename,
                    size_bytes=len(rebranded_bytes),
                    version="rebranded",
                )
                db.session.add(new_form)
                db.session.flush()   # get new_form.id
                saved_ids.append(new_form.id)

                # ── Add to ZIP ──────────────────────────────────
                zip_entry = f"{base_name}_rebranded.pdf"
                zf.writestr(zip_entry, rebranded_bytes)

            except Exception as exc:
                logger.error("rebrand: failed for Form #%d — %s", fid, exc, exc_info=True)
                errors.append(f"Form #{fid} ({form.name}): {exc}")

        db.session.commit()

    zip_buf.seek(0)
    return {
        "saved_form_ids": saved_ids,
        "zip_bytes": zip_buf.read(),
        "errors": errors,
    }
