# extractor.py
import io
from typing import Dict, Any, List

import pdfplumber
from pypdf import PdfReader
from engine.ocr_config import configure_tesseract

# Try to import pytesseract - optional, only needed for OCR
pytesseract = None
try:
    import pytesseract as _pytesseract
    pytesseract = _pytesseract
except ImportError:
    pytesseract = None

# Configure tesseract if available
configure_tesseract()



def extract_text_from_pdf(file_bytes: bytes) -> str:
    """Extract embedded text from a PDF (works for digital PDFs, not scanned images)."""
    text_chunks: List[str] = []
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text() or ""
            if page_text.strip():
                text_chunks.append(page_text.strip())
    return "\n\n".join(text_chunks)


def extract_form_fields_from_pdf(file_bytes: bytes) -> dict:
    """Extract AcroForm fields if present (works only for fillable PDFs)."""
    reader = PdfReader(io.BytesIO(file_bytes))
    try:
        fields = reader.get_form_text_fields()
    except Exception:
        fields = {}
    return fields or {}


def ocr_pdf_to_text(file_bytes: bytes, dpi: int = 300, lang: str = "eng") -> Dict[str, Any]:
    """
    OCR fallback for scanned/printed PDFs.
    Requires:
      pip install pytesseract pillow
      and Tesseract OCR installed in the OS/container.

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
                # Render to image at desired DPI
                img = page.to_image(resolution=dpi).original

                # Light preprocessing (keeps it safe; helps on many scans)
                img = img.convert("L")  # grayscale

                txt = (pytesseract.image_to_string(img, lang=lang) or "").strip()
                per_page.append({"page": idx, "text": txt})

                if txt:
                    combined.append(f"[Page {idx}]\n{txt}")
    except Exception as e:
        print(f"WARNING: OCR processing failed: {e}")
        return {"text": "", "pages": []}

    return {"text": "\n\n".join(combined), "pages": per_page}


def extract_all(file_bytes: bytes, enable_ocr_fallback: bool = True, ocr_dpi: int = 300) -> Dict[str, Any]:
    """
    Returns a dict for the LLM:
      - text: embedded text OR OCR text (if scanned)
      - fields: AcroForm fields if present
      - pages: per-page OCR text (empty for non-OCR)
      - meta: flags for scanned/OCR usage
    """
    text = extract_text_from_pdf(file_bytes)
    fields = extract_form_fields_from_pdf(file_bytes)

    is_scanned_like = (not text.strip()) and (not fields)

    ocr_payload = {"text": "", "pages": []}
    if enable_ocr_fallback and is_scanned_like:
        ocr_payload = ocr_pdf_to_text(file_bytes, dpi=ocr_dpi)

    final_text = text.strip() or ocr_payload.get("text", "").strip()

    return {
        "text": final_text,
        "fields": fields,
        "pages": ocr_payload.get("pages", []),
        "meta": {
            "is_scanned_like": is_scanned_like,
            "used_ocr": bool(ocr_payload.get("text", "").strip()),
            "ocr_dpi": ocr_dpi if bool(ocr_payload.get("text", "").strip()) else None,
        },
    }
