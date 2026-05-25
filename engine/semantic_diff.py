"""
Semantic Diff Engine — Stage 2 of the FAST v2 pipeline.

Rather than comparing pixel values or raw text strings, this module:
  1. Builds a structured diff of two ParsedDocuments (field changes, table
     changes, text changes, structural changes)
  2. Sends that structured diff to an LLM for semantic interpretation
  3. Returns high-confidence observations with context

Why this is better than the current approach:
  - Operates on structured content (tables as tables, fields as fields)
  - Semantic comparison: "Effective Date changed from X to Y" not "10 pixels differ"
  - LLM receives a focused, dense diff rather than full raw text
  - Far fewer false positives from layout noise
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from engine.document_parser import ParsedDocument

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Diff data structures
# ---------------------------------------------------------------------------

@dataclass
class FieldChange:
    field_name: str
    baseline_value: str
    current_value: str
    page: int = 0
    change_type: str = "modified"    # "added" | "removed" | "modified"


@dataclass
class TableChange:
    page: int
    table_index: int
    baseline_headers: list[str]
    current_headers: list[str]
    added_rows: list[list[str]]
    removed_rows: list[list[str]]
    modified_rows: list[dict[str, Any]]   # {row_idx, baseline, current}


@dataclass
class TextChange:
    page: int
    change_type: str               # "added" | "removed" | "modified"
    baseline_text: str
    current_text: str
    context: str = ""              # surrounding unchanged text for context


@dataclass
class StructuralChange:
    change_type: str               # "page_added" | "page_removed" | "page_reordered"
    detail: str


@dataclass
class StructuredDiff:
    """Complete structural diff between two documents."""
    field_changes: list[FieldChange] = field(default_factory=list)
    table_changes: list[TableChange] = field(default_factory=list)
    text_changes: list[TextChange] = field(default_factory=list)
    structural_changes: list[StructuralChange] = field(default_factory=list)
    page_count_changed: bool = False
    baseline_pages: int = 0
    current_pages: int = 0

    def is_empty(self) -> bool:
        return (
            not self.field_changes
            and not self.table_changes
            and not self.text_changes
            and not self.structural_changes
        )

    def summary_text(self) -> str:
        parts = []
        if self.page_count_changed:
            parts.append(
                f"Page count: {self.baseline_pages} → {self.current_pages}"
            )
        if self.field_changes:
            parts.append(f"{len(self.field_changes)} form field change(s)")
        if self.table_changes:
            parts.append(f"{len(self.table_changes)} table change(s)")
        if self.text_changes:
            parts.append(f"{len(self.text_changes)} text change(s)")
        if self.structural_changes:
            parts.append(f"{len(self.structural_changes)} structural change(s)")
        return "; ".join(parts) if parts else "No changes detected"


# ---------------------------------------------------------------------------
# Structural diff builders
# ---------------------------------------------------------------------------

def diff_form_fields(baseline: ParsedDocument, current: ParsedDocument) -> list[FieldChange]:
    """Compare form fields between two documents."""
    b_fields = baseline.all_form_fields()
    c_fields = current.all_form_fields()

    changes: list[FieldChange] = []
    all_keys = set(b_fields) | set(c_fields)

    for key in sorted(all_keys):
        b_val = b_fields.get(key, "")
        c_val = c_fields.get(key, "")
        if b_val == c_val:
            continue
        if not b_val:
            changes.append(FieldChange(key, "", c_val, change_type="added"))
        elif not c_val:
            changes.append(FieldChange(key, b_val, "", change_type="removed"))
        else:
            changes.append(FieldChange(key, b_val, c_val, change_type="modified"))

    return changes


def diff_tables(baseline: ParsedDocument, current: ParsedDocument) -> list[TableChange]:
    """Compare tables between matching pages."""
    changes: list[TableChange] = []
    b_pages = {p.page_num: p for p in baseline.pages}
    c_pages = {p.page_num: p for p in current.pages}

    common_pages = set(b_pages) & set(c_pages)
    for page_num in sorted(common_pages):
        b_page = b_pages[page_num]
        c_page = c_pages[page_num]

        max_tables = max(len(b_page.tables), len(c_page.tables))
        for ti in range(max_tables):
            b_tbl = b_page.tables[ti] if ti < len(b_page.tables) else None
            c_tbl = c_page.tables[ti] if ti < len(c_page.tables) else None

            if b_tbl is None and c_tbl is None:
                continue
            b_rows = set(tuple(r) for r in (b_tbl["rows"] if b_tbl else []))
            c_rows = set(tuple(r) for r in (c_tbl["rows"] if c_tbl else []))

            added = [list(r) for r in c_rows - b_rows]
            removed = [list(r) for r in b_rows - c_rows]

            if added or removed or (b_tbl and c_tbl and b_tbl["headers"] != c_tbl["headers"]):
                changes.append(TableChange(
                    page=page_num,
                    table_index=ti,
                    baseline_headers=b_tbl["headers"] if b_tbl else [],
                    current_headers=c_tbl["headers"] if c_tbl else [],
                    added_rows=added[:20],     # cap to avoid token explosion
                    removed_rows=removed[:20],
                    modified_rows=[],
                ))

    return changes


def diff_page_text(baseline_text: str, current_text: str, page: int) -> list[TextChange]:
    """
    Line-level diff between two pages of text.
    Returns only genuinely changed lines with context.
    """
    import difflib

    b_lines = [l.strip() for l in baseline_text.splitlines() if l.strip()]
    c_lines = [l.strip() for l in current_text.splitlines() if l.strip()]

    changes: list[TextChange] = []
    matcher = difflib.SequenceMatcher(None, b_lines, c_lines, autojunk=False)

    for op, b0, b1, c0, c1 in matcher.get_opcodes():
        if op == "equal":
            continue
        b_chunk = " | ".join(b_lines[b0:b1])[:400]
        c_chunk = " | ".join(c_lines[c0:c1])[:400]

        if op == "replace":
            changes.append(TextChange(page, "modified", b_chunk, c_chunk))
        elif op == "delete":
            changes.append(TextChange(page, "removed", b_chunk, ""))
        elif op == "insert":
            changes.append(TextChange(page, "added", "", c_chunk))

    return changes


def build_structured_diff(baseline: ParsedDocument, current: ParsedDocument) -> StructuredDiff:
    """Build a complete StructuredDiff between two ParsedDocuments."""
    sd = StructuredDiff(
        baseline_pages=baseline.page_count,
        current_pages=current.page_count,
        page_count_changed=baseline.page_count != current.page_count,
    )

    sd.field_changes = diff_form_fields(baseline, current)
    sd.table_changes = diff_tables(baseline, current)

    if sd.page_count_changed:
        sd.structural_changes.append(StructuralChange(
            change_type="page_count_changed",
            detail=f"Baseline has {baseline.page_count} pages; current has {current.page_count} pages",
        ))

    # Text diff for each matched page
    b_map = {p.page_num: p for p in baseline.pages}
    c_map = {p.page_num: p for p in current.pages}

    for pg in sorted(set(b_map) & set(c_map)):
        b_text = b_map[pg].text
        c_text = c_map[pg].text
        if b_text != c_text:
            changes = diff_page_text(b_text, c_text, pg)
            # Only include pages with meaningful changes (filter noise)
            meaningful = [
                ch for ch in changes
                if len((ch.baseline_text + ch.current_text).split()) > 2
            ]
            sd.text_changes.extend(meaningful[:15])   # cap per page

    # Detect added/removed pages
    b_only = set(b_map) - set(c_map)
    c_only = set(c_map) - set(b_map)
    for pg in sorted(b_only):
        sd.structural_changes.append(StructuralChange("page_removed", f"Page {pg} removed"))
    for pg in sorted(c_only):
        sd.structural_changes.append(StructuralChange("page_added", f"Page {pg} added"))

    return sd


# ---------------------------------------------------------------------------
# LLM semantic interpretation
# ---------------------------------------------------------------------------

_SEMANTIC_SYSTEM = """You are an expert document analyst comparing two versions of an insurance or financial PDF form.

You will receive a STRUCTURED DIFF showing exactly what changed between the BASELINE (original) and CURRENT (under review) versions.

Your job is to interpret these changes and produce clear, factual observations.

RULES:
- One observation per logical change (not per raw diff line)
- Group related field changes on the same page into one observation
- State the actual values: "Effective Date changed from 10/01/2025 to 10/15/2025"
- Note removed signatures, added watermarks, page count changes
- Confidence: "certain" = clearly a data/text change, "likely" = probable change, "possible" = ambiguous
- Do NOT invent changes not present in the diff
- Do NOT comment on formatting whitespace unless it changes meaning

Respond ONLY with valid JSON:
{
  "observations": [
    {
      "current_page": "1",
      "observation": "Plain English description of the change.",
      "confidence": "certain | likely | possible",
      "change_type": "field_change | text_change | table_change | structural | signature | watermark"
    }
  ],
  "overall_summary": "One sentence summarizing all differences.",
  "change_count": 3
}"""


def _build_diff_prompt(sd: StructuredDiff, max_chars: int = 8000) -> str:
    """Serialize StructuredDiff into a compact, LLM-readable prompt block."""
    parts: list[str] = []

    if sd.structural_changes:
        parts.append("=== STRUCTURAL CHANGES ===")
        for sc in sd.structural_changes:
            parts.append(f"  • {sc.change_type}: {sc.detail}")

    if sd.field_changes:
        parts.append("\n=== FORM FIELD CHANGES ===")
        for fc in sd.field_changes:
            if fc.change_type == "modified":
                parts.append(f"  • {fc.field_name}: '{fc.baseline_value}' → '{fc.current_value}'")
            elif fc.change_type == "added":
                parts.append(f"  • {fc.field_name}: [NEW] = '{fc.current_value}'")
            else:
                parts.append(f"  • {fc.field_name}: [REMOVED] was '{fc.baseline_value}'")

    if sd.table_changes:
        parts.append("\n=== TABLE CHANGES ===")
        for tc in sd.table_changes:
            parts.append(f"  Page {tc.page}, Table {tc.table_index + 1}:")
            if tc.baseline_headers != tc.current_headers:
                parts.append(f"    Headers changed: {tc.baseline_headers} → {tc.current_headers}")
            for row in tc.added_rows[:5]:
                parts.append(f"    + ROW ADDED: {row}")
            for row in tc.removed_rows[:5]:
                parts.append(f"    - ROW REMOVED: {row}")

    if sd.text_changes:
        parts.append("\n=== TEXT CHANGES ===")
        for tc in sd.text_changes:
            if tc.change_type == "modified":
                parts.append(f"  Page {tc.page}:")
                parts.append(f"    WAS: {tc.baseline_text[:200]}")
                parts.append(f"    NOW: {tc.current_text[:200]}")
            elif tc.change_type == "removed":
                parts.append(f"  Page {tc.page} — REMOVED: {tc.baseline_text[:200]}")
            elif tc.change_type == "added":
                parts.append(f"  Page {tc.page} — ADDED: {tc.current_text[:200]}")

    full = "\n".join(parts)
    # Truncate to token budget
    return full[:max_chars] if len(full) > max_chars else full


def run_semantic_diff(
    baseline: ParsedDocument,
    current: ParsedDocument,
    llm_fn: Any,
    include_vision: bool = False,
    vision_images: list[dict] | None = None,
) -> dict[str, Any]:
    """
    Run a full semantic diff between two documents.

    Args:
        baseline: Parsed baseline document.
        current: Parsed current document.
        llm_fn: Callable(messages) -> dict — the LLM runner.
        include_vision: Whether to attach page images alongside the text diff.
        vision_images: List of {page, b64, mime, label} for vision mode.

    Returns:
        Dict with observations[], overall_summary, change_count, structured_diff.
    """
    from config import diff_config
    cfg = diff_config()

    sd = build_structured_diff(baseline, current)

    if sd.is_empty():
        return {
            "observations": [],
            "overall_summary": "No differences detected between the two documents.",
            "change_count": 0,
            "structured_diff_summary": "No changes",
        }

    diff_text = _build_diff_prompt(sd, max_chars=cfg.max_doc_tokens)

    user_content: list[dict] | str
    if include_vision and vision_images:
        # Attach a sample of page images alongside the structured diff for
        # visual confirmation (e.g. watermarks that don't appear in text layer)
        blocks: list[dict] = [
            {"type": "text", "text": f"STRUCTURED DIFF:\n{diff_text}\n\nVISUAL EVIDENCE (selected pages):"}
        ]
        for img in (vision_images or [])[:10]:   # limit to 10 images
            blocks.append({
                "type": "image",
                "mime": img["mime"],
                "b64":  img["b64"],
                "label": img.get("label", ""),
            })
        user_content = blocks
    else:
        user_content = f"STRUCTURED DIFF:\n{diff_text}"

    messages = [
        {"role": "system", "content": _SEMANTIC_SYSTEM},
        {"role": "user", "content": user_content},
    ]

    try:
        result = llm_fn(messages)
    except Exception as exc:
        logger.error("Semantic diff LLM call failed: %s", exc)
        result = {
            "observations": _fallback_observations(sd),
            "overall_summary": sd.summary_text(),
            "change_count": len(sd.field_changes) + len(sd.text_changes),
        }

    # Attach the raw structured diff for transparency
    result["structured_diff_summary"] = sd.summary_text()
    result["diff_stats"] = {
        "field_changes": len(sd.field_changes),
        "table_changes": len(sd.table_changes),
        "text_changes": len(sd.text_changes),
        "structural_changes": len(sd.structural_changes),
    }
    return result


def _fallback_observations(sd: StructuredDiff) -> list[dict[str, Any]]:
    """Generate basic observations directly from StructuredDiff without LLM."""
    obs: list[dict[str, Any]] = []

    for sc in sd.structural_changes:
        obs.append({
            "current_page": "all",
            "observation": sc.detail,
            "confidence": "certain",
            "change_type": "structural",
        })

    for fc in sd.field_changes[:20]:
        if fc.change_type == "modified":
            text = f"Form field '{fc.field_name}' changed from '{fc.baseline_value}' to '{fc.current_value}'"
        elif fc.change_type == "added":
            text = f"Form field '{fc.field_name}' added with value '{fc.current_value}'"
        else:
            text = f"Form field '{fc.field_name}' removed (was '{fc.baseline_value}')"
        obs.append({
            "current_page": str(fc.page or "unknown"),
            "observation": text,
            "confidence": "certain",
            "change_type": "field_change",
        })

    for tc in sd.text_changes[:10]:
        text = f"Page {tc.page}: text changed"
        if tc.baseline_text:
            text += f" — was: '{tc.baseline_text[:100]}'"
        if tc.current_text:
            text += f", now: '{tc.current_text[:100]}'"
        obs.append({
            "current_page": str(tc.page),
            "observation": text,
            "confidence": "likely",
            "change_type": "text_change",
        })

    return obs
