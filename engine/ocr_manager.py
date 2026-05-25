"""
OCR Manager — intelligent routing for scanned and mixed-content PDFs.

Rather than blindly running Tesseract on everything, this module:
  1. Classifies each page: DIGITAL | SCANNED | MIXED
  2. Chooses the right extraction strategy per page
  3. Post-processes OCR output for insurance-form patterns

Strategy priority:
  LLAMAPARSE  → highest accuracy, handles forms + tables natively, AI OCR
  VISION_LLM  → send page image to Claude/GPT-4V for extraction
  TESSERACT   → local fallback, fast, adequate for clean scans
"""
from __future__ import annotations

import base64
import io
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable

logger = logging.getLogger(__name__)


class PageType(Enum):
    DIGITAL = "digital"       # has dense text layer — no OCR needed
    SCANNED = "scanned"       # no text layer — full OCR required
    MIXED   = "mixed"         # partial text layer — selective OCR
    BLANK   = "blank"         # empty or near-empty page


@dataclass
class OCRPageResult:
    page_num: int
    page_type: PageType
    text: str
    confidence: float          # 0.0 – 1.0
    strategy_used: str         # "digital" | "llamaparse" | "vision_llm" | "tesseract"
    word_count: int


@dataclass
class OCRDocumentResult:
    pages: list[OCRPageResult]
    overall_type: PageType     # document-level classification
    strategies_used: set[str]


# ---------------------------------------------------------------------------
# Page classification
# ---------------------------------------------------------------------------

def classify_page(text: str, image_ratio: float = 0.0) -> PageType:
    """
    Classify a PDF page based on extracted text density.

    Args:
        text: Text already extracted from the page (may be empty for scanned).
        image_ratio: Ratio of image area to total page area (0–1).
    """
    words = len(text.split())
    if words == 0:
        return PageType.SCANNED if image_ratio > 0.3 else PageType.BLANK
    if words < 30:
        return PageType.MIXED
    return PageType.DIGITAL


def classify_document(page_texts: list[str]) -> PageType:
    """Document-level classification based on majority of pages."""
    if not page_texts:
        return PageType.SCANNED
    counts = {t: 0 for t in PageType}
    for txt in page_texts:
        counts[classify_page(txt)] += 1
    total = len(page_texts)
    scanned_ratio = counts[PageType.SCANNED] / total
    mixed_ratio = counts[PageType.MIXED] / total

    blank_ratio = counts[PageType.BLANK] / total
    # Blank pages (no text, no image_ratio context) are treated as scanned at doc level
    if scanned_ratio + blank_ratio > 0.5:
        return PageType.SCANNED
    if scanned_ratio + mixed_ratio > 0.3:
        return PageType.MIXED
    return PageType.DIGITAL


# ---------------------------------------------------------------------------
# Strategy: Vision LLM OCR
# ---------------------------------------------------------------------------

def _ocr_via_vision_llm(img_bytes: bytes, page_num: int, llm_fn: Callable) -> OCRPageResult:
    """
    Use a vision LLM (Claude/GPT-4V) to extract text from a page image.

    Advantages over Tesseract:
    - Understands context (knows "eff. date" = Effective Date)
    - Handles handwritten fields
    - Preserves table structure
    - Works on low-DPI scans that Tesseract fails on
    """
    b64 = base64.standard_b64encode(img_bytes).decode()
    messages = [
        {
            "role": "system",
            "content": (
                "You are an expert OCR assistant specializing in insurance and financial forms. "
                "Extract ALL text exactly as it appears. "
                "For form fields, output: 'Field Label: field value' per line. "
                "For tables, use markdown table format. "
                "For checkboxes: '[X] Option Name' if checked, '[ ] Option Name' if unchecked. "
                "For signatures: output '[SIGNATURE]'. "
                "Do not interpret, summarize, or add commentary."
            ),
        },
        {
            "role": "user",
            "content": [
                {"type": "text", "text": f"Extract all text from this form page (page {page_num}):"},
                {"type": "image", "mime": "image/png", "b64": b64, "label": f"Page {page_num}"},
            ],
        },
    ]

    try:
        raw = llm_fn(messages)
        text = raw.get("text", "") if isinstance(raw, dict) else str(raw)
        words = len(text.split())
        confidence = min(1.0, words / 50)  # proxy: more words = higher confidence
        return OCRPageResult(
            page_num=page_num,
            page_type=PageType.SCANNED,
            text=text.strip(),
            confidence=confidence,
            strategy_used="vision_llm",
            word_count=words,
        )
    except Exception as exc:
        logger.warning("Vision LLM OCR failed page %d: %s", page_num, exc)
        return OCRPageResult(
            page_num=page_num,
            page_type=PageType.SCANNED,
            text="",
            confidence=0.0,
            strategy_used="vision_llm_failed",
            word_count=0,
        )


# ---------------------------------------------------------------------------
# Strategy: Tesseract OCR
# ---------------------------------------------------------------------------

def _ocr_via_tesseract(img_bytes: bytes, page_num: int) -> OCRPageResult:
    """
    Tesseract fallback OCR.

    Applies preprocessing to improve accuracy:
    - Grayscale conversion
    - Contrast enhancement
    - Deskew (if pytesseract supports it)

    Works best on high-DPI (300+) clean scans.
    """
    try:
        import pytesseract
        from PIL import Image, ImageEnhance, ImageFilter
    except ImportError:
        return OCRPageResult(
            page_num=page_num,
            page_type=PageType.SCANNED,
            text="",
            confidence=0.0,
            strategy_used="tesseract_unavailable",
            word_count=0,
        )

    try:
        img = Image.open(io.BytesIO(img_bytes))

        # Preprocessing pipeline for insurance forms
        img = img.convert("L")                              # grayscale
        img = ImageEnhance.Contrast(img).enhance(1.5)      # boost contrast
        img = img.filter(ImageFilter.SHARPEN)               # sharpen edges

        # Tesseract with LSTM engine + form-optimized config
        config = "--oem 3 --psm 6 -c preserve_interword_spaces=1"
        data = pytesseract.image_to_data(img, config=config, output_type=pytesseract.Output.DICT)

        # Filter low-confidence words (< 30%)
        words = [
            w for w, conf in zip(data["text"], data["conf"])
            if str(w).strip() and int(conf) > 30
        ]
        text = " ".join(words)
        avg_conf = (
            sum(int(c) for c in data["conf"] if int(c) > 0) /
            max(1, sum(1 for c in data["conf"] if int(c) > 0))
        ) / 100.0

        return OCRPageResult(
            page_num=page_num,
            page_type=PageType.SCANNED,
            text=text.strip(),
            confidence=avg_conf,
            strategy_used="tesseract",
            word_count=len(words),
        )
    except Exception as exc:
        logger.warning("Tesseract OCR failed page %d: %s", page_num, exc)
        return OCRPageResult(
            page_num=page_num,
            page_type=PageType.SCANNED,
            text="",
            confidence=0.0,
            strategy_used="tesseract_failed",
            word_count=0,
        )


# ---------------------------------------------------------------------------
# Strategy: digital pass-through
# ---------------------------------------------------------------------------

def _digital_page(page_num: int, text: str) -> OCRPageResult:
    words = len(text.split())
    return OCRPageResult(
        page_num=page_num,
        page_type=PageType.DIGITAL,
        text=text,
        confidence=1.0,
        strategy_used="digital",
        word_count=words,
    )


# ---------------------------------------------------------------------------
# Post-processing for insurance forms
# ---------------------------------------------------------------------------

# Common insurance form field normalizations
_FIELD_ALIASES: dict[str, str] = {
    "eff. date": "Effective Date",
    "eff date": "Effective Date",
    "pol. no.": "Policy Number",
    "pol no": "Policy Number",
    "named insured": "Named Insured",
    "n. insured": "Named Insured",
    "prem.": "Premium",
    "min. prem.": "Minimum Premium",
    "policywriting min. prem.": "Policywriting Minimum Premium",
}


def normalize_insurance_text(text: str) -> str:
    """Normalize common OCR variations in insurance form text."""
    import re

    # Normalize field aliases
    result = text
    for abbr, full in _FIELD_ALIASES.items():
        result = re.sub(re.escape(abbr), full, result, flags=re.IGNORECASE)

    # Fix common OCR errors in insurance context
    result = re.sub(r"\b0(?=[A-Z])", "O", result)          # 0 → O before letters
    result = re.sub(r"(?<=[A-Z])0\b", "O", result)         # O → O after letters
    result = re.sub(r"\bl(?=\d)", "1", result)              # l → 1 before digits
    result = re.sub(r"(\$)\s+(\d)", r"\1\2", result)        # $ 100 → $100
    result = re.sub(r"\s+", " ", result)                    # collapse whitespace

    return result.strip()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def process_document_ocr(
    pdf_bytes: bytes,
    existing_page_texts: list[str],
    llm_fn: Callable | None = None,
    render_dpi: int = 300,
) -> OCRDocumentResult:
    """
    Smart OCR processing for a PDF document.

    Only runs OCR on pages that need it (SCANNED or MIXED).
    Digital pages are passed through unchanged.

    Args:
        pdf_bytes: Raw PDF bytes.
        existing_page_texts: Text already extracted per page (may be empty strings).
        llm_fn: Vision LLM callable for high-accuracy OCR on difficult pages.
        render_dpi: DPI to use when rendering pages for OCR.

    Returns:
        OCRDocumentResult with per-page results and strategies used.
    """
    doc_type = classify_document(existing_page_texts)
    results: list[OCRPageResult] = []
    strategies: set[str] = set()

    # For fully digital docs, skip OCR entirely
    if doc_type == PageType.DIGITAL:
        for i, text in enumerate(existing_page_texts, start=1):
            r = _digital_page(i, text)
            results.append(r)
            strategies.add(r.strategy_used)
        return OCRDocumentResult(pages=results, overall_type=doc_type, strategies_used=strategies)

    # Need page images for scanned/mixed
    page_images = _render_pdf_pages(pdf_bytes, render_dpi)

    for i, (text, img_bytes) in enumerate(
        zip(existing_page_texts, page_images), start=1
    ):
        ptype = classify_page(text)

        if ptype == PageType.DIGITAL:
            r = _digital_page(i, text)
        elif ptype == PageType.BLANK:
            r = OCRPageResult(
                page_num=i, page_type=PageType.BLANK, text="",
                confidence=1.0, strategy_used="blank", word_count=0,
            )
        else:
            # Scanned or mixed: run best available OCR
            if llm_fn:
                r = _ocr_via_vision_llm(img_bytes, i, llm_fn)
                if r.word_count < 10:
                    # Vision LLM got very little — try Tesseract as sanity check
                    tess = _ocr_via_tesseract(img_bytes, i)
                    if tess.word_count > r.word_count:
                        r = tess
            else:
                r = _ocr_via_tesseract(img_bytes, i)

            # Post-process insurance-specific text
            r.text = normalize_insurance_text(r.text)

        results.append(r)
        strategies.add(r.strategy_used)

    return OCRDocumentResult(pages=results, overall_type=doc_type, strategies_used=strategies)


def _render_pdf_pages(pdf_bytes: bytes, dpi: int) -> list[bytes]:
    """Render all PDF pages to PNG bytes for OCR processing."""
    images: list[bytes] = []

    # Try PyMuPDF first (fastest, no external dep)
    try:
        import fitz
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        scale = dpi / 72.0
        matrix = fitz.Matrix(scale, scale)
        for page in doc:
            pix = page.get_pixmap(matrix=matrix)
            images.append(pix.tobytes("png"))
        doc.close()
        return images
    except ImportError:
        pass

    # Fallback: pdf2image + poppler
    try:
        from pdf2image import convert_from_bytes
        pil_images = convert_from_bytes(pdf_bytes, dpi=dpi, fmt="PNG")
        for img in pil_images:
            buf = io.BytesIO()
            img.save(buf, "PNG")
            images.append(buf.getvalue())
        return images
    except Exception as exc:
        logger.warning("Page rendering failed: %s", exc)
        return []
