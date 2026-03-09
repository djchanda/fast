import io
from typing import Dict, Any, List

import pdfplumber
from pypdf import PdfReader

from engine.ocr_config import configure_tesseract

pytesseract = None
try:
    import pytesseract as _pytesseract
    pytesseract = _pytesseract
except ImportError:
    pytesseract = None

configure_tesseract()


def extract_text_and_pages_from_pdf(file_bytes: bytes) -> Dict[str, Any]:
    """
    Extract embedded text from a PDF and always return per-page text
    for digital PDFs as well.
    """
    text_chunks: List[str] = []
    pages: List[Dict[str, Any]] = []

    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for idx, page in enumerate(pdf.pages, start=1):
            page_text = (page.extract_text() or "").strip()
            pages.append(
                {
                    "page": idx,
                    "text": page_text,
                    "source": "embedded",
                }
            )
            if page_text:
                text_chunks.append(f"[Page {idx}]\n{page_text}")

    return {
        "text": "\n\n".join(text_chunks).strip(),
        "pages": pages,
    }


def extract_form_fields_from_pdf(file_bytes: bytes) -> dict:
    """Extract AcroForm fields if present."""
    reader = PdfReader(io.BytesIO(file_bytes))
    try:
        fields = reader.get_form_text_fields()
    except Exception:
        fields = {}
    return fields or {}


def extract_page_visual_inventory(file_bytes: bytes) -> List[Dict[str, Any]]:
    """
    Lightweight per-page visual inventory.
    This is not a full CV pass, but it gives the LLM useful hints.
    """
    inventory: List[Dict[str, Any]] = []

    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for idx, page in enumerate(pdf.pages, start=1):
            words = page.extract_words() or []
            word_text = " ".join(w.get("text", "") for w in words).upper()

            has_president = "PRESIDENT" in word_text
            has_secretary = "SECRETARY" in word_text
            has_signature = "SIGNATURE" in word_text
            has_named_insured = "NAMED INSURED" in word_text

            inventory.append(
                {
                    "page": idx,
                    "image_count": len(getattr(page, "images", []) or []),
                    "line_count": len(getattr(page, "lines", []) or []),
                    "rect_count": len(getattr(page, "rects", []) or []),
                    "curve_count": len(getattr(page, "curves", []) or []),
                    "word_count": len(words),
                    "has_president_label": has_president,
                    "has_secretary_label": has_secretary,
                    "has_signature_label": has_signature,
                    "has_signature_block": has_president or has_secretary or has_signature,
                    "has_named_insured_label": has_named_insured,
                }
            )

    return inventory


def ocr_pdf_to_text(file_bytes: bytes, dpi: int = 300, lang: str = "eng") -> Dict[str, Any]:
    """
    OCR fallback for scanned/printed PDFs.
    Returns empty dict if OCR is not available.
    """
    if pytesseract is None:
        print("WARNING: pytesseract not available. Skipping OCR.")
        return {"text": "", "pages": []}

    try:
        from PIL import Image  # noqa: F401
    except Exception as e:
        print(f"WARNING: Pillow not available. Skipping OCR: {e}")
        return {"text": "", "pages": []}

    per_page: List[Dict[str, Any]] = []
    combined: List[str] = []

    try:
        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            for idx, page in enumerate(pdf.pages, start=1):
                img = page.to_image(resolution=dpi).original
                img = img.convert("L")

                txt = (pytesseract.image_to_string(img, lang=lang) or "").strip()
                per_page.append({"page": idx, "text": txt, "source": "ocr"})

                if txt:
                    combined.append(f"[Page {idx}]\n{txt}")
    except Exception as e:
        print(f"WARNING: OCR processing failed: {e}")
        return {"text": "", "pages": []}

    return {"text": "\n\n".join(combined), "pages": per_page}


def extract_all(file_bytes: bytes, enable_ocr_fallback: bool = True, ocr_dpi: int = 300) -> Dict[str, Any]:
    """
    Returns a dict for the LLM:
      - text: embedded text OR OCR text
      - fields: AcroForm fields if present
      - pages: per-page text for both digital and OCR docs
      - page_visual_inventory: light visual hints per page
      - meta: flags for scanned/OCR usage
    """
    embedded_payload = extract_text_and_pages_from_pdf(file_bytes)
    text = embedded_payload.get("text", "")
    pages = embedded_payload.get("pages", [])
    fields = extract_form_fields_from_pdf(file_bytes)
    visual_inventory = extract_page_visual_inventory(file_bytes)

    is_scanned_like = (not text.strip()) and (not fields)

    ocr_payload = {"text": "", "pages": []}
    if enable_ocr_fallback and is_scanned_like:
        ocr_payload = ocr_pdf_to_text(file_bytes, dpi=ocr_dpi)

    final_text = text.strip() or ocr_payload.get("text", "").strip()
    final_pages = pages if pages else ocr_payload.get("pages", [])

    return {
        "text": final_text,
        "fields": fields,
        "pages": final_pages,
        "page_visual_inventory": visual_inventory,
        "meta": {
            "is_scanned_like": is_scanned_like,
            "used_ocr": bool(ocr_payload.get("text", "").strip()),
            "ocr_dpi": ocr_dpi if bool(ocr_payload.get("text", "").strip()) else None,
            "page_count": len(final_pages),
        },
    }