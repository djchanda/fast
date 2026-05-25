"""
Document Parser — Stage 1 of the FAST v2 pipeline.

Responsibility: turn raw PDF bytes into a structured ParsedDocument that
downstream stages (semantic diff, vision pipeline) can reason about.

Backend priority:
  1. LlamaParse  (cloud, AI-powered, best OCR + table extraction)
  2. PyMuPDF + Vision LLM  (local, good for digital PDFs with complex layout)
  3. pdfplumber  (local, fast, adequate for simple text-heavy PDFs)

The caller never needs to pick a backend — the manager selects automatically
based on what API keys are available and what the document looks like.
"""
from __future__ import annotations

import io
import logging
import os
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class PageContent:
    page_num: int
    text: str                          # full text of the page
    tables: list[dict[str, Any]]       # list of {headers: [...], rows: [[...]]}
    form_fields: dict[str, str]        # {field_label: value}
    has_images: bool = False
    has_signature: bool = False
    source: str = "unknown"            # "llamaparse" | "pymupdf" | "pdfplumber" | "ocr"


@dataclass
class ParsedDocument:
    pages: list[PageContent]
    raw_markdown: str = ""             # full LlamaParse markdown output
    form_fields: dict[str, str] = field(default_factory=dict)   # doc-level AcroForm fields
    page_count: int = 0
    backend_used: str = "unknown"
    is_scanned: bool = False
    ocr_applied: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def full_text(self) -> str:
        return "\n\n".join(f"[Page {p.page_num}]\n{p.text}" for p in self.pages if p.text.strip())

    def page_texts(self) -> list[dict[str, Any]]:
        return [{"page": p.page_num, "text": p.text, "source": p.source} for p in self.pages]

    def all_tables(self) -> list[dict[str, Any]]:
        tables = []
        for p in self.pages:
            for t in p.tables:
                tables.append({"page": p.page_num, **t})
        return tables

    def all_form_fields(self) -> dict[str, str]:
        merged = dict(self.form_fields)
        for p in self.pages:
            merged.update(p.form_fields)
        return merged


# ---------------------------------------------------------------------------
# Backend: LlamaParse
# ---------------------------------------------------------------------------

def _parse_llamaparse(pdf_bytes: bytes, cfg) -> ParsedDocument:
    """
    Use LlamaParse cloud API to extract structured markdown.

    LlamaParse handles:
    - Digital PDFs (preserving tables, columns, headers)
    - Scanned PDFs (AI OCR — far better than Tesseract for forms)
    - Mixed content (partial scans, embedded images with text)

    Returns a ParsedDocument with rich per-page content.
    """
    try:
        from llama_parse import LlamaParse
        from llama_index.core import SimpleDirectoryReader
    except ImportError as e:
        raise ImportError(
            "llama-parse not installed. Run: pip install llama-parse llama-index-core"
        ) from e

    import tempfile

    parser = LlamaParse(
        api_key=cfg.llamaparse_api_key,
        result_type=cfg.llamaparse_result_type,
        premium_mode=cfg.llamaparse_premium_mode,
        language=cfg.llamaparse_language,
        verbose=False,
        # Tell LlamaParse to treat this as a form so it preserves field structure
        parsing_instruction=(
            "This is an insurance or financial PDF form. "
            "Preserve all form field labels and their values. "
            "Extract all tables with their headers and rows intact. "
            "Preserve column structure and multi-column layouts. "
            "Mark signature areas with [SIGNATURE BLOCK]. "
            "For scanned content, OCR every character precisely."
        ),
    )

    # LlamaParse works with file paths, not bytes — write to temp file
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(pdf_bytes)
        tmp_path = tmp.name

    try:
        documents = parser.load_data(tmp_path)
    finally:
        os.unlink(tmp_path)

    if not documents:
        raise ValueError("LlamaParse returned no documents")

    # LlamaParse returns one Document per page in some modes,
    # or a single Document with page markers. Handle both.
    full_markdown = "\n\n".join(d.text for d in documents)
    pages = _split_llamaparse_pages(full_markdown, documents)

    return ParsedDocument(
        pages=pages,
        raw_markdown=full_markdown,
        page_count=len(pages),
        backend_used="llamaparse",
        is_scanned=_detect_scan(pages, cfg),
        ocr_applied=True,   # LlamaParse always applies OCR when needed
        metadata={"source": "llamaparse", "doc_count": len(documents)},
    )


def _split_llamaparse_pages(markdown: str, documents) -> list[PageContent]:
    """
    Split LlamaParse output into per-page PageContent objects.
    LlamaParse marks page boundaries with '---' or page headers.
    """
    import re

    pages: list[PageContent] = []

    # Check if each document is already one page
    if len(documents) > 1:
        for i, doc in enumerate(documents, start=1):
            pages.append(_parse_page_markdown(i, doc.text))
        return pages

    # Single document — split on page markers
    # LlamaParse uses patterns like "## Page 1" or "---\n" or "\f"
    page_blocks = re.split(
        r"(?:^|\n)(?:---+|## Page \d+|\f)",
        markdown,
        flags=re.MULTILINE,
    )

    for i, block in enumerate(page_blocks, start=1):
        if block.strip():
            pages.append(_parse_page_markdown(i, block))

    return pages if pages else [_parse_page_markdown(1, markdown)]


def _parse_page_markdown(page_num: int, markdown: str) -> PageContent:
    """Extract structured content from a page's markdown."""
    tables = _extract_markdown_tables(markdown)
    form_fields = _extract_form_fields_from_markdown(markdown)
    has_sig = bool(
        "[SIGNATURE BLOCK]" in markdown.upper()
        or "SIGNATURE" in markdown.upper()
        or "PRESIDENT" in markdown.upper()
        or "SECRETARY" in markdown.upper()
    )
    # Clean up markdown syntax for plain text
    import re
    plain = re.sub(r"[#*`|_~]", "", markdown)
    plain = re.sub(r"\n{3,}", "\n\n", plain).strip()

    return PageContent(
        page_num=page_num,
        text=plain,
        tables=tables,
        form_fields=form_fields,
        has_signature=has_sig,
        source="llamaparse",
    )


def _extract_markdown_tables(markdown: str) -> list[dict[str, Any]]:
    """Parse markdown pipe tables into structured dicts."""
    import re
    tables = []
    # Match markdown table blocks: header row | separator row | data rows
    table_pattern = re.compile(
        r"(\|[^\n]+\|\n\|[-| :]+\|\n(?:\|[^\n]+\|\n?)+)",
        re.MULTILINE,
    )
    for match in table_pattern.finditer(markdown):
        lines = [l.strip() for l in match.group(0).strip().split("\n") if l.strip()]
        if len(lines) < 2:
            continue
        headers = [c.strip() for c in lines[0].split("|") if c.strip()]
        rows = []
        for line in lines[2:]:  # skip separator
            row = [c.strip() for c in line.split("|") if c.strip() != ""]
            if row:
                rows.append(row)
        if headers and rows:
            tables.append({"headers": headers, "rows": rows})
    return tables


def _extract_form_fields_from_markdown(markdown: str) -> dict[str, str]:
    """
    Extract form field key-value pairs from LlamaParse markdown.
    LlamaParse renders filled fields as "Field Label: value" or in tables.
    """
    import re
    fields: dict[str, str] = {}
    # Pattern: "Label Name: value" (common in insurance forms)
    for match in re.finditer(r"^([A-Z][A-Za-z /\-()]{2,50})\s*:\s*(.{1,200})$", markdown, re.MULTILINE):
        label = match.group(1).strip()
        value = match.group(2).strip()
        # Filter out markdown section headers and table content
        if len(value) > 0 and not value.startswith("|") and "---" not in value:
            fields[label] = value
    return fields


# ---------------------------------------------------------------------------
# Backend: PyMuPDF + Vision LLM
# ---------------------------------------------------------------------------

def _parse_pymupdf_vision(pdf_bytes: bytes, cfg, llm_fn=None) -> ParsedDocument:
    """
    PyMuPDF for text + layout, optionally enriched with a vision-LLM pass
    for pages that appear scanned or have complex tables.

    Good balance: works locally, no cloud dependency, vision LLM only called
    for pages that need it (scanned/complex).
    """
    try:
        import fitz  # PyMuPDF
    except ImportError as e:
        raise ImportError("PyMuPDF not installed. Run: pip install pymupdf") from e

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    pages: list[PageContent] = []
    ocr_applied = False

    for page_idx in range(len(doc)):
        page = doc[page_idx]
        page_num = page_idx + 1

        # Extract text blocks with position info
        text_dict = page.get_text("dict")
        blocks = text_dict.get("blocks", [])
        text_lines = []
        form_fields: dict[str, str] = {}

        for block in blocks:
            if block.get("type") == 0:  # text block
                for line in block.get("lines", []):
                    line_text = " ".join(
                        span.get("text", "") for span in line.get("spans", [])
                    ).strip()
                    if line_text:
                        text_lines.append(line_text)

        plain_text = "\n".join(text_lines)

        # Extract AcroForm fields for this page
        fields_raw = doc.get_fields(page_num - 1) if hasattr(doc, "get_fields") else {}
        if fields_raw:
            for fname, fdata in fields_raw.items():
                if isinstance(fdata, dict):
                    val = fdata.get("value", "")
                    if val:
                        form_fields[fname] = str(val)

        # Detect sparse pages → need OCR
        words_on_page = len(plain_text.split())
        needs_ocr = words_on_page < cfg.scanned_word_threshold

        source = "pymupdf"
        if needs_ocr and llm_fn:
            # Render page as image and ask LLM to extract text
            matrix = fitz.Matrix(cfg.ocr_dpi / 72, cfg.ocr_dpi / 72)
            pix = page.get_pixmap(matrix=matrix)
            img_bytes = pix.tobytes("png")
            ocr_text = _ocr_page_via_llm(img_bytes, page_num, llm_fn)
            if ocr_text.strip():
                plain_text = ocr_text
                source = "vision_ocr"
                ocr_applied = True

        # Extract tables via PyMuPDF's table finder
        tables = _extract_pymupdf_tables(page)

        has_sig = "signature" in plain_text.lower() or "president" in plain_text.lower()

        pages.append(PageContent(
            page_num=page_num,
            text=plain_text,
            tables=tables,
            form_fields=form_fields,
            has_images=any(b.get("type") == 1 for b in blocks),
            has_signature=has_sig,
            source=source,
        ))

    doc.close()

    return ParsedDocument(
        pages=pages,
        page_count=len(pages),
        backend_used="pymupdf_vision",
        is_scanned=_detect_scan(pages, cfg),
        ocr_applied=ocr_applied,
        metadata={"source": "pymupdf"},
    )


def _extract_pymupdf_tables(page) -> list[dict[str, Any]]:
    """Use PyMuPDF's built-in table detector."""
    tables = []
    try:
        tab_finder = page.find_tables()
        for tab in tab_finder.tables:
            extracted = tab.extract()
            if not extracted:
                continue
            headers = extracted[0] if extracted else []
            rows = extracted[1:] if len(extracted) > 1 else []
            if headers:
                tables.append({
                    "headers": [str(h or "").strip() for h in headers],
                    "rows": [[str(c or "").strip() for c in row] for row in rows],
                })
    except Exception:
        pass
    return tables


def _ocr_page_via_llm(img_bytes: bytes, page_num: int, llm_fn) -> str:
    """
    Send a scanned page image to the LLM for text extraction.
    More accurate than Tesseract for forms with complex layout.
    """
    import base64

    b64 = base64.standard_b64encode(img_bytes).decode()
    messages = [
        {
            "role": "system",
            "content": (
                "You are an OCR assistant. Extract ALL text from this page image exactly "
                "as it appears. Preserve form field labels and their values. "
                "Format as: 'Label: value' per line. Do not summarize or interpret."
            ),
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": f"Extract all text from page {page_num}:",
                },
                {
                    "type": "image",
                    "mime": "image/png",
                    "b64": b64,
                    "label": f"Page {page_num}",
                },
            ],
        },
    ]
    try:
        result = llm_fn(messages)
        if isinstance(result, dict):
            return result.get("text", result.get("raw_response", ""))
        return str(result)
    except Exception as exc:
        logger.warning("Vision OCR failed for page %d: %s", page_num, exc)
        return ""


# ---------------------------------------------------------------------------
# Backend: pdfplumber (fallback)
# ---------------------------------------------------------------------------

def _parse_pdfplumber(pdf_bytes: bytes, cfg) -> ParsedDocument:
    """
    pdfplumber fallback — adequate for simple text-heavy digital PDFs.
    Does NOT handle scanned PDFs well.
    """
    import pdfplumber
    from pypdf import PdfReader

    pages: list[PageContent] = []

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for idx, page in enumerate(pdf.pages, start=1):
            text = (page.extract_text() or "").strip()
            tables = []
            for tbl in (page.extract_tables() or []):
                if tbl and tbl[0]:
                    tables.append({
                        "headers": [str(c or "") for c in tbl[0]],
                        "rows": [[str(c or "") for c in row] for row in tbl[1:]],
                    })
            pages.append(PageContent(
                page_num=idx,
                text=text,
                tables=tables,
                form_fields={},
                source="pdfplumber",
            ))

    # AcroForm fields via pypdf
    acro_fields: dict[str, str] = {}
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        raw = reader.get_form_text_fields() or {}
        acro_fields = {k: str(v) for k, v in raw.items() if v}
    except Exception:
        pass

    return ParsedDocument(
        pages=pages,
        form_fields=acro_fields,
        page_count=len(pages),
        backend_used="pdfplumber",
        is_scanned=_detect_scan(pages, cfg),
        ocr_applied=False,
    )


# ---------------------------------------------------------------------------
# Scan detection helper
# ---------------------------------------------------------------------------

def _detect_scan(pages: list[PageContent], cfg) -> bool:
    """Return True if the document appears to be a scanned image (not digital text)."""
    if not pages:
        return False
    word_counts = [len(p.text.split()) for p in pages]
    avg = sum(word_counts) / len(word_counts)
    return avg < cfg.scanned_word_threshold


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_pdf(pdf_bytes: bytes, llm_fn=None) -> ParsedDocument:
    """
    Parse a PDF into a structured ParsedDocument.

    Backend selection:
    1. LlamaParse  — if LLAMA_CLOUD_API_KEY is set (best accuracy)
    2. PyMuPDF + Vision LLM  — if llm_fn provided (good local option)
    3. pdfplumber  — final fallback

    Args:
        pdf_bytes: Raw PDF bytes.
        llm_fn: Optional callable(messages) -> dict for vision-OCR on sparse pages.
                When provided, scanned pages are sent to the LLM for extraction.

    Returns:
        ParsedDocument with per-page text, tables, form fields, and metadata.
    """
    from config import parser_config
    cfg = parser_config()

    # 1. LlamaParse (cloud, best accuracy)
    if cfg.llamaparse_api_key:
        try:
            logger.info("Parsing PDF with LlamaParse (premium_mode=%s)", cfg.llamaparse_premium_mode)
            return _parse_llamaparse(pdf_bytes, cfg)
        except Exception as exc:
            logger.warning("LlamaParse failed (%s) — falling back", exc)

    # 2. PyMuPDF + optional vision OCR
    try:
        logger.info("Parsing PDF with PyMuPDF (vision_ocr=%s)", llm_fn is not None)
        return _parse_pymupdf_vision(pdf_bytes, cfg, llm_fn=llm_fn)
    except ImportError:
        logger.warning("PyMuPDF not available — falling back to pdfplumber")
    except Exception as exc:
        logger.warning("PyMuPDF failed (%s) — falling back to pdfplumber", exc)

    # 3. pdfplumber (always available, last resort)
    logger.info("Parsing PDF with pdfplumber (fallback)")
    return _parse_pdfplumber(pdf_bytes, cfg)
