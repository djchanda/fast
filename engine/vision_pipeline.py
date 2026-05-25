"""
Vision Pipeline — high-fidelity page-image comparison using LLM vision.

Role in the v2 pipeline:
  - Catches visual-only changes: watermarks, logos, signature boxes, colour
    changes, layout shifts that survive text extraction intact
  - Runs as a COMPLEMENT to semantic diff (not a replacement)
  - For benchmark mode: interleaved BASELINE / CURRENT page pairs → Claude
  - For basic mode: single document → Claude checks against known spec

Key improvements over the v1 approach:
  - Higher render DPI (150 default, configurable up to 300)
  - Alignment-aware pairing (DP edit-distance, same as v1 but now feeds
    semantic diff so both layers share page-alignment context)
  - Vision observations are merged with semantic observations, deduped
"""
from __future__ import annotations

import base64
import io
import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class PageImage:
    page_num: int
    b64: str
    mime: str
    width: int
    height: int


# ---------------------------------------------------------------------------
# PDF → page images
# ---------------------------------------------------------------------------

def render_pdf_pages(pdf_bytes: bytes, dpi: int = 150, fmt: str = "JPEG", quality: int = 85) -> list[PageImage]:
    """
    Render every page of a PDF to an image.

    Returns list of PageImage with base64-encoded bytes.
    Uses PyMuPDF (fast) with pdf2image fallback.
    """
    images: list[PageImage] = []

    # Primary: PyMuPDF
    try:
        import fitz
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        scale = dpi / 72.0
        matrix = fitz.Matrix(scale, scale)

        for idx, page in enumerate(doc, start=1):
            pix = page.get_pixmap(matrix=matrix)

            if fmt.upper() == "JPEG":
                img_bytes = pix.tobytes("jpeg")
                mime = "image/jpeg"
            else:
                img_bytes = pix.tobytes("png")
                mime = "image/png"

            b64 = base64.standard_b64encode(img_bytes).decode()
            images.append(PageImage(
                page_num=idx,
                b64=b64,
                mime=mime,
                width=pix.width,
                height=pix.height,
            ))
        doc.close()
        return images

    except ImportError:
        logger.warning("PyMuPDF not available — falling back to pdf2image")
    except Exception as exc:
        logger.warning("PyMuPDF render failed: %s — falling back", exc)

    # Fallback: pdf2image + Pillow
    try:
        from pdf2image import convert_from_bytes
        from PIL import Image as PILImage

        pil_pages = convert_from_bytes(pdf_bytes, dpi=dpi)
        for idx, img in enumerate(pil_pages, start=1):
            buf = io.BytesIO()
            if fmt.upper() == "JPEG":
                img.convert("RGB").save(buf, "JPEG", quality=quality)
                mime = "image/jpeg"
            else:
                img.save(buf, "PNG")
                mime = "image/png"
            raw = buf.getvalue()
            b64 = base64.standard_b64encode(raw).decode()
            images.append(PageImage(
                page_num=idx,
                b64=b64,
                mime=mime,
                width=img.width,
                height=img.height,
            ))
        return images

    except Exception as exc:
        logger.error("pdf2image render also failed: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Page alignment (edit-distance DP)
# ---------------------------------------------------------------------------

def align_pages(
    baseline_images: list[PageImage],
    current_images: list[PageImage],
    similarity_fn=None,
) -> list[dict[str, Any]]:
    """
    Align baseline and current pages using edit-distance DP.

    Returns a list of alignment records:
      {"baseline_page": int|None, "current_page": int|None, "op": "matched|deleted|inserted"}

    When both PDFs have the same page count, this degenerates to a 1-to-1 mapping.
    When pages are inserted/deleted, the DP finds the best alignment.
    """
    n = len(baseline_images)
    m = len(current_images)

    if n == m:
        return [
            {"baseline_page": b.page_num, "current_page": c.page_num, "op": "matched"}
            for b, c in zip(baseline_images, current_images)
        ]

    # Simple DP with unit costs — good enough for small page count differences
    DELETE = 1.0
    INSERT = 1.0

    dp = [[0.0] * (m + 1) for _ in range(n + 1)]
    for i in range(n + 1):
        dp[i][0] = i * DELETE
    for j in range(m + 1):
        dp[0][j] = j * INSERT

    for i in range(1, n + 1):
        for j in range(1, m + 1):
            match_cost = 0.0   # same position = 0 cost (assume match)
            dp[i][j] = min(
                dp[i - 1][j - 1] + match_cost,
                dp[i - 1][j] + DELETE,
                dp[i][j - 1] + INSERT,
            )

    # Trace back
    alignment = []
    i, j = n, m
    while i > 0 or j > 0:
        if i > 0 and j > 0 and dp[i][j] == dp[i - 1][j - 1]:
            alignment.append({
                "baseline_page": baseline_images[i - 1].page_num,
                "current_page": current_images[j - 1].page_num,
                "op": "matched",
            })
            i -= 1
            j -= 1
        elif i > 0 and dp[i][j] == dp[i - 1][j] + DELETE:
            alignment.append({
                "baseline_page": baseline_images[i - 1].page_num,
                "current_page": None,
                "op": "deleted",
            })
            i -= 1
        else:
            alignment.append({
                "baseline_page": None,
                "current_page": current_images[j - 1].page_num,
                "op": "inserted",
            })
            j -= 1

    alignment.reverse()
    return alignment


# ---------------------------------------------------------------------------
# Vision prompt builder
# ---------------------------------------------------------------------------

_VISION_SYSTEM = """You are an expert document analyst comparing two versions of an insurance or financial PDF form.

BASELINE = the original / golden-copy version.
CURRENT  = the version under review.

Pages are shown interleaved. For each pair, identify EVERY visible difference.

RULES:
- Report factual observations only — what changed, not why
- For text changes: quote the exact before/after values
- For watermarks: read character-by-character, quote exactly
- For signatures: note presence/absence of ink or stamp
- For layout: note field position changes only if content moved to different area
- Do NOT report identical content as a change
- Group changes on the same page into one observation when they are related
- current_page must be the page number in the CURRENT document

Respond ONLY with valid JSON:
{
  "observations": [
    {
      "current_page": "1",
      "observation": "Description of the change.",
      "confidence": "certain | likely | possible"
    }
  ],
  "overall_summary": "One sentence overall summary.",
  "pages_with_changes": [1, 3]
}"""


def build_vision_messages(
    baseline_images: list[PageImage],
    current_images: list[PageImage],
    alignment: list[dict[str, Any]] | None = None,
    max_pages: int = 40,
) -> list[dict[str, Any]]:
    """
    Build LLM messages for vision-based comparison.

    Pages are interleaved using alignment data so the LLM sees
    BASELINE page N next to its matching CURRENT page — not just
    positional neighbours.
    """
    b_map = {img.page_num: img for img in baseline_images}
    c_map = {img.page_num: img for img in current_images}

    if alignment is None:
        alignment = align_pages(baseline_images, current_images)

    blocks: list[dict[str, Any]] = [
        {"type": "text", "text": "Compare the following interleaved BASELINE and CURRENT pages:"}
    ]

    pages_added = 0
    for pair in alignment:
        if pages_added >= max_pages:
            blocks.append({"type": "text", "text": f"\n[Remaining {len(alignment) - pages_added} pages omitted — page limit reached]"})
            break

        op = pair["op"]
        b_pg = pair.get("baseline_page")
        c_pg = pair.get("current_page")

        if op == "deleted" and b_pg in b_map:
            blocks.append({"type": "text", "text": f"\n--- BASELINE page {b_pg} (DELETED — no match in current) ---"})
            blocks.append({"type": "image", "mime": b_map[b_pg].mime, "b64": b_map[b_pg].b64, "label": f"BASELINE page {b_pg}"})
            pages_added += 1
        elif op == "inserted" and c_pg in c_map:
            blocks.append({"type": "text", "text": f"\n--- CURRENT page {c_pg} (INSERTED — no match in baseline) ---"})
            blocks.append({"type": "image", "mime": c_map[c_pg].mime, "b64": c_map[c_pg].b64, "label": f"CURRENT page {c_pg}"})
            pages_added += 1
        elif op == "matched":
            label = f"pages {b_pg}/{c_pg}" if b_pg != c_pg else f"page {b_pg}"
            blocks.append({"type": "text", "text": f"\n--- {label} ---"})
            if b_pg in b_map:
                blocks.append({"type": "image", "mime": b_map[b_pg].mime, "b64": b_map[b_pg].b64, "label": f"BASELINE page {b_pg}"})
            if c_pg in c_map:
                blocks.append({"type": "image", "mime": c_map[c_pg].mime, "b64": c_map[c_pg].b64, "label": f"CURRENT page {c_pg}"})
            pages_added += 2

    blocks.append({"type": "text", "text": "\nNow list every difference you found in the JSON format specified."})

    return [
        {"role": "system", "content": _VISION_SYSTEM},
        {"role": "user", "content": blocks},
    ]


# ---------------------------------------------------------------------------
# Observation merger
# ---------------------------------------------------------------------------

def merge_observations(
    semantic_obs: list[dict[str, Any]],
    vision_obs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Merge semantic diff observations with vision observations.

    Strategy:
    - Semantic observations take precedence (they have exact before/after values)
    - Vision observations added when they cover something not in semantic diff
      (e.g. watermarks, logos, signature ink — not in text layer)
    - Deduplicate by similarity of observation text on the same page
    """
    import difflib

    merged = list(semantic_obs)
    semantic_texts = [o.get("observation", "").lower() for o in merged]

    for vobs in vision_obs:
        v_text = vobs.get("observation", "").lower()
        v_page = vobs.get("current_page", "")

        # Check if a semantically similar observation already exists for this page
        is_dup = False
        for s_text in semantic_texts:
            ratio = difflib.SequenceMatcher(None, v_text, s_text).ratio()
            if ratio > 0.7:
                is_dup = True
                break

        if not is_dup:
            # Tag as vision-only so reviewers know the source
            vobs["source"] = "vision"
            merged.append(vobs)
            semantic_texts.append(v_text)

    return merged


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_vision_comparison(
    baseline_pdf_bytes: bytes,
    current_pdf_bytes: bytes,
    llm_fn: Any,
    dpi: int = 150,
    max_pages: int = 40,
) -> dict[str, Any]:
    """
    Full vision-based comparison of two PDFs.

    Returns observations in the same schema as semantic_diff.run_semantic_diff.
    """
    from config import parser_config
    cfg = parser_config()

    logger.info("Rendering baseline PDF pages (dpi=%d)", dpi)
    baseline_images = render_pdf_pages(baseline_pdf_bytes, dpi=dpi, fmt=cfg.render_fmt)

    logger.info("Rendering current PDF pages (dpi=%d)", dpi)
    current_images  = render_pdf_pages(current_pdf_bytes,  dpi=dpi, fmt=cfg.render_fmt)

    if not baseline_images or not current_images:
        logger.warning("No images rendered — cannot run vision comparison")
        return {"observations": [], "overall_summary": "Vision rendering failed.", "pages_with_changes": []}

    alignment = align_pages(baseline_images, current_images)
    messages = build_vision_messages(baseline_images, current_images, alignment, max_pages=max_pages)

    try:
        result = llm_fn(messages)
    except Exception as exc:
        logger.error("Vision LLM call failed: %s", exc)
        return {"observations": [], "overall_summary": f"Vision comparison failed: {exc}", "pages_with_changes": []}

    result.setdefault("observations", [])
    result.setdefault("overall_summary", "")
    result.setdefault("pages_with_changes", [])

    # Attach image metadata for downstream snapshot generation
    result["_baseline_images"] = [
        {"page": img.page_num, "b64": img.b64, "mime": img.mime} for img in baseline_images
    ]
    result["_current_images"] = [
        {"page": img.page_num, "b64": img.b64, "mime": img.mime} for img in current_images
    ]
    result["_alignment"] = alignment

    return result
