"""
Main Pipeline — drop-in replacement for app/services/runner.py logic.

Orchestrates:
  parse  →  semantic_diff  →  vision_pipeline  →  merge  →  schema

The output JSON schema is identical to v1 so the Flask app needs zero changes.

Three modes (same as v1):
  benchmark — compare two PDFs (main vs baseline), full semantic + vision
  basic     — validate one PDF against general rules
  specific  — validate one PDF against a specific checklist / rules set
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

from engine.document_parser import parse_pdf, ParsedDocument
from engine.ocr_manager import process_document_ocr
from engine.semantic_diff import run_semantic_diff, build_structured_diff
from engine.vision_pipeline import render_pdf_pages, run_vision_comparison, merge_observations
from engine.llm_client import run_llm, make_llm_fn

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Output schema (identical to v1 for zero-change Flask integration)
# ---------------------------------------------------------------------------

_BASE_SCHEMA: dict[str, Any] = {
    "mode": "",
    "observations": [],
    "spelling_errors": [],
    "format_issues": [],
    "value_mismatches": [],
    "missing_content": [],
    "extra_content": [],
    "layout_anomalies": [],
    "typography_issues": [],
    "structural_changes": [],
    "compliance_issues": [],
    "visual_mismatches": [],
    "accessibility_issues": [],
    "summary_counts": {},
    "pages_impacted": [],
    "top_findings": [],
    "overall_summary": "",
    "accuracy_score": 0,
    "visual_validation": [],
    "error": "",
    # v2 additions (ignored by v1 Flask code, additive only)
    "engine_version": "2.0",
    "backends_used": [],
    "diff_stats": {},
}


def _schema(mode: str, **overrides) -> dict[str, Any]:
    out = dict(_BASE_SCHEMA)
    out["mode"] = mode
    out.update(overrides)
    return out


# ---------------------------------------------------------------------------
# Benchmark mode (main vs baseline — the primary use case)
# ---------------------------------------------------------------------------

def run_benchmark(
    main_pdf_bytes: bytes,
    bench_pdf_bytes: bytes,
    *,
    instance_path: str = "",
    result_id: str = "",
    project_id: int = 0,
    test_case_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Compare two PDFs semantically + visually.

    Stage 1: Parse both PDFs with LlamaParse (or fallback)
    Stage 2: Build structured diff (fields, tables, text)
    Stage 3: LLM interprets the structured diff → semantic observations
    Stage 4: Vision pipeline renders pages → LLM visual comparison
    Stage 5: Merge semantic + vision observations, deduplicate
    Stage 6: Generate visual_validation snapshots for the UI
    """
    from config import diff_config, parser_config
    cfg_diff   = diff_config()
    cfg_parser = parser_config()

    llm_fn = make_llm_fn()
    backends: list[str] = []

    # ── Stage 1: Parse ────────────────────────────────────────────────────
    logger.info("[benchmark] Parsing main PDF")
    main_doc = parse_pdf(main_pdf_bytes, llm_fn=llm_fn)
    backends.append(main_doc.backend_used)

    logger.info("[benchmark] Parsing baseline PDF")
    bench_doc = parse_pdf(bench_pdf_bytes, llm_fn=llm_fn)
    backends.append(bench_doc.backend_used)

    logger.info("[benchmark] backends: %s", backends)

    # ── Stage 2 + 3: Semantic diff ────────────────────────────────────────
    logger.info("[benchmark] Running semantic diff")
    sem_result = run_semantic_diff(
        baseline=bench_doc,
        current=main_doc,
        llm_fn=llm_fn,
    )
    sem_observations = sem_result.get("observations", [])
    diff_stats       = sem_result.get("diff_stats", {})
    semantic_summary = sem_result.get("overall_summary", "")

    # ── Stage 4: Vision pipeline ──────────────────────────────────────────
    logger.info("[benchmark] Running vision comparison")
    vis_result = run_vision_comparison(
        baseline_pdf_bytes=bench_pdf_bytes,
        current_pdf_bytes=main_pdf_bytes,
        llm_fn=llm_fn,
        dpi=cfg_parser.render_dpi,
        max_pages=cfg_diff.max_image_pages,
    )
    vis_observations = vis_result.get("observations", [])
    vis_summary      = vis_result.get("overall_summary", "")

    # ── Stage 5: Merge ────────────────────────────────────────────────────
    all_observations = merge_observations(sem_observations, vis_observations)
    overall_summary  = semantic_summary or vis_summary

    # ── Stage 6: Visual snapshots for UI ─────────────────────────────────
    visual_validation = _build_visual_validation(
        vis_result=vis_result,
        instance_path=instance_path,
        result_id=result_id,
        project_id=project_id,
    )

    # ── Assemble output schema ────────────────────────────────────────────
    pages_impacted = sorted({
        _parse_page_ref(o.get("current_page", ""))
        for o in all_observations
        if o.get("current_page")
    } - {0})

    return _schema(
        "benchmark",
        observations=all_observations,
        overall_summary=overall_summary,
        accuracy_score=_accuracy_score(all_observations),
        pages_impacted=pages_impacted,
        visual_validation=visual_validation,
        backends_used=list(set(backends)),
        diff_stats=diff_stats,
    )


# ---------------------------------------------------------------------------
# Basic mode (single PDF, general rules)
# ---------------------------------------------------------------------------

_BASIC_SYSTEM = """You are an expert insurance forms analyst.
Review the provided form and identify ALL issues: spelling errors, format problems,
missing required fields, layout anomalies, typography issues, accessibility issues.

Respond ONLY with valid JSON matching this schema exactly:
{
  "spelling_errors": [{"page": 1, "text": "mispeled", "suggestion": "misspelled"}],
  "format_issues": [{"page": 1, "description": "Date not in MM/DD/YYYY format"}],
  "missing_content": [{"page": 1, "description": "Signature line missing"}],
  "layout_anomalies": [{"page": 1, "description": "Footer text overlaps body"}],
  "typography_issues": [{"page": 1, "description": "Inconsistent font size in headers"}],
  "accessibility_issues": [{"page": 1, "description": "Low contrast text in section 3"}],
  "overall_summary": "Summary of findings.",
  "accuracy_score": 85
}"""


def run_basic(
    pdf_bytes: bytes,
    *,
    instance_path: str = "",
    result_id: str = "",
    project_id: int = 0,
    test_case_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate a single PDF against general quality rules."""
    llm_fn = make_llm_fn()

    doc = parse_pdf(pdf_bytes, llm_fn=llm_fn)
    page_images = render_pdf_pages(pdf_bytes, dpi=120, fmt="JPEG")

    # Build prompt with text + page images
    text_block = _truncate(doc.full_text(), 6000)
    fields = doc.all_form_fields()
    tables = doc.all_tables()

    user_blocks: list[dict] = [
        {"type": "text", "text": (
            f"DOCUMENT TEXT:\n{text_block}\n\n"
            f"FORM FIELDS: {json.dumps(fields, indent=2)[:1000]}\n"
            f"TABLES: {len(tables)} table(s) found\n\n"
            "PAGE IMAGES follow for visual verification:"
        )}
    ]
    for img in page_images[:20]:
        user_blocks.append({"type": "image", "mime": img.mime, "b64": img.b64, "label": f"Page {img.page_num}"})
    user_blocks.append({"type": "text", "text": "Now identify all issues in the JSON format specified."})

    messages = [
        {"role": "system", "content": _BASIC_SYSTEM},
        {"role": "user", "content": user_blocks},
    ]

    result = llm_fn(messages)

    return _schema(
        "basic",
        spelling_errors=result.get("spelling_errors", []),
        format_issues=result.get("format_issues", []),
        missing_content=result.get("missing_content", []),
        layout_anomalies=result.get("layout_anomalies", []),
        typography_issues=result.get("typography_issues", []),
        accessibility_issues=result.get("accessibility_issues", []),
        overall_summary=result.get("overall_summary", ""),
        accuracy_score=result.get("accuracy_score", 0),
        backends_used=[doc.backend_used],
    )


# ---------------------------------------------------------------------------
# Specific mode (checklist-driven validation)
# ---------------------------------------------------------------------------

_SPECIFIC_SYSTEM_TMPL = """You are an expert insurance forms analyst.
Validate the form against this specific checklist:
{checklist}

For each checklist item, report PASS or FAIL with evidence.

Respond ONLY with valid JSON:
{{
  "value_mismatches": [{{"page": 1, "field": "...", "expected": "...", "actual": "..."}}],
  "compliance_issues": [{{"page": 1, "rule": "...", "status": "FAIL", "detail": "..."}}],
  "missing_content": [{{"page": 1, "description": "..."}}],
  "overall_summary": "...",
  "accuracy_score": 85,
  "passed_checks": 10,
  "failed_checks": 2
}}"""


def run_specific(
    pdf_bytes: bytes,
    *,
    checklist: list[str] | None = None,
    instance_path: str = "",
    result_id: str = "",
    project_id: int = 0,
    test_case_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate a PDF against a specific rules checklist."""
    llm_fn = make_llm_fn()
    checklist = checklist or (test_case_config or {}).get("checklist", [])
    checklist_text = "\n".join(f"  {i+1}. {item}" for i, item in enumerate(checklist)) if checklist else "  (no checklist provided)"

    doc = parse_pdf(pdf_bytes, llm_fn=llm_fn)
    page_images = render_pdf_pages(pdf_bytes, dpi=120, fmt="JPEG")

    text_block = _truncate(doc.full_text(), 6000)
    user_blocks: list[dict] = [
        {"type": "text", "text": (
            f"DOCUMENT TEXT:\n{text_block}\n\n"
            "PAGE IMAGES for visual verification:"
        )}
    ]
    for img in page_images[:20]:
        user_blocks.append({"type": "image", "mime": img.mime, "b64": img.b64, "label": f"Page {img.page_num}"})
    user_blocks.append({"type": "text", "text": "Validate against the checklist. Respond in the JSON format specified."})

    system = _SPECIFIC_SYSTEM_TMPL.format(checklist=checklist_text)
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_blocks},
    ]

    result = llm_fn(messages)

    return _schema(
        "specific",
        value_mismatches=result.get("value_mismatches", []),
        compliance_issues=result.get("compliance_issues", []),
        missing_content=result.get("missing_content", []),
        overall_summary=result.get("overall_summary", ""),
        accuracy_score=result.get("accuracy_score", 0),
        backends_used=[doc.backend_used],
    )


# ---------------------------------------------------------------------------
# Visual snapshot builder (for the v1 UI diff viewer)
# ---------------------------------------------------------------------------

def _build_visual_validation(
    vis_result: dict[str, Any],
    instance_path: str,
    result_id: str,
    project_id: int,
) -> list[dict[str, Any]]:
    """
    Save side-by-side diff snapshots to disk and return visual_validation rows
    in the same format as v1 so the existing UI works unchanged.
    """
    from PIL import Image as PILImage
    import base64
    import io as _io

    if not instance_path or not result_id:
        return []

    output_dir = os.path.join(instance_path, "visual_diffs")
    os.makedirs(output_dir, exist_ok=True)

    baseline_images = {
        img["page"]: img
        for img in vis_result.get("_baseline_images", [])
    }
    current_images = {
        img["page"]: img
        for img in vis_result.get("_current_images", [])
    }
    alignment = vis_result.get("_alignment", [])
    rows: list[dict[str, Any]] = []

    for pair in alignment:
        op      = pair["op"]
        b_pg    = pair.get("baseline_page")
        c_pg    = pair.get("current_page")
        row_pg  = c_pg or b_pg

        b_img_data = baseline_images.get(b_pg) if b_pg else None
        c_img_data = current_images.get(c_pg)  if c_pg else None

        if not b_img_data and not c_img_data:
            continue

        try:
            panels = []
            for img_data in (b_img_data, c_img_data):
                if img_data:
                    raw = base64.standard_b64decode(img_data["b64"])
                    panels.append(PILImage.open(_io.BytesIO(raw)).convert("RGB"))

            if not panels:
                continue

            # Compose 3-panel: baseline | diff overlay | current
            if len(panels) == 2:
                b_pil, c_pil = panels
                w = min(b_pil.width, c_pil.width)
                h = min(b_pil.height, c_pil.height)
                b_pil = b_pil.resize((w, h), PILImage.LANCZOS)
                c_pil = c_pil.resize((w, h), PILImage.LANCZOS)

                from PIL import ImageChops, ImageEnhance
                diff = ImageChops.difference(b_pil, c_pil)
                diff = ImageEnhance.Contrast(diff).enhance(5.0)

                panel = PILImage.new("RGB", (w * 3, h))
                panel.paste(b_pil, (0, 0))
                panel.paste(diff,  (w, 0))
                panel.paste(c_pil, (w * 2, 0))
            else:
                panel = panels[0]

            fname = f"{result_id}_page{row_pg:03d}_diff.png"
            fpath = os.path.join(output_dir, fname)
            panel.save(fpath, "PNG")

            rows.append({
                "page": row_pg,
                "actual_page_num": c_pg,
                "expected_page_num": b_pg,
                "alignment_op": op,
                "snapshot_path": fname,
                "diff_bbox": None,
                "similarity": 1.0 if op == "matched" else 0.0,
                "major": op in ("deleted", "inserted"),
                "warn": False,
            })
        except Exception as exc:
            logger.warning("Snapshot generation failed page %s: %s", row_pg, exc)

    return rows


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_page_ref(ref: str) -> int:
    """Extract first integer from a page reference like '1', '2-3', 'all'."""
    import re
    if str(ref).lower() == "all":
        return 0
    m = re.search(r"\d+", str(ref))
    return int(m.group()) if m else 0


def _accuracy_score(observations: list[dict]) -> int:
    """Simple proxy: 100 minus penalty per observation by confidence."""
    penalties = {"certain": 10, "likely": 6, "possible": 3}
    total = sum(penalties.get(o.get("confidence", "possible"), 3) for o in observations)
    return max(0, 100 - total)


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n... [truncated {len(text) - max_chars} chars]"
