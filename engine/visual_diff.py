# engine/visual_diff.py
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pdfplumber
from pdf2image import convert_from_path
from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFont

logger = logging.getLogger(__name__)

POPPLER_BIN = os.getenv("POPPLER_PATH", r"C:\Poppler\Release-25.12.0-0\poppler-25.12.0\Library\bin")

# Costs used by the edit-distance page-alignment DP.
# A match is preferred when  sim(i,j)  >  (DELETE_COST + INSERT_COST) / (2 * MATCH_SCALE).
# With defaults (1.0 / 1.0 / 2.0) pages must be > 50 % similar to be considered a match.
_ALIGN_DELETE_COST: float = 1.0
_ALIGN_INSERT_COST: float = 1.0
_ALIGN_MATCH_SCALE: float = 2.0   # amplifier so mismatch cost = scale*(1-sim)


def _poppler_path() -> Optional[str]:
    """
    Use env-configured Poppler if available.
    If not found, return None and let pdf2image try system PATH.
    """
    if POPPLER_BIN and os.path.isdir(POPPLER_BIN):
        return POPPLER_BIN
    return None


class VisualDiff:
    """
    Generates visual diffs between two PDFs.

    Precision-first behavior:
    - Signature detection is restricted to strong signature-zone evidence.
    - Generic warned pages are not automatically signature defects.
    - We keep visual evidence rich, but avoid over-classifying noise.
    """

    # Tightened list: removed AUTHORIZED because it was causing false positives
    SIGNATURE_KEYWORDS = {"PRESIDENT", "SECRETARY", "SIGNATURE"}

    def __init__(self, output_dir: Optional[str | Path] = None):
        if output_dir is None:
            output_dir = Path.cwd() / "visual_diffs"
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def compare_pdfs_detailed(
        self,
        original_pdf_path: str,
        expected_pdf_path: str,
        result_id: Optional[str] = None,
        dpi: int = 200,
    ) -> List[Dict[str, Any]]:
        """
        Returns per-page visual comparison rows using sequence alignment.

        When the two PDFs have different page counts (e.g. pages were removed or
        inserted), a DP edit-distance alignment is performed first so that pages
        are compared against their true counterparts rather than their positional
        neighbours.  Deleted / inserted pages receive their own rows with
        alignment_op = "deleted" | "inserted" so that downstream LLM prompts and
        reports can surface the structural change explicitly.

        Output row shape:
        {
            "page": 3,                        # sequential output row number
            "expected_page_num": 3,           # page number in the expected PDF
            "actual_page_num":   3,           # page number in the actual PDF (None if deleted)
            "alignment_op": "matched",        # "matched" | "deleted" | "inserted"
            "similarity": 0.998,
            "major": False,
            "warn": True,
            "note": "...",
            "snapshot_path": "...",
            "diff_bbox": [x0, y0, x1, y1] | None,
            "diff_pixels_pct": 0.1234,
            "diff_area_pct": 0.0,
            "signature_candidate": bool,
            "signature_label": "PRESIDENT" | None,
            "signature_reason": "...",
            "signature_confidence": "high|medium|low|none"
        }
        """
        try:
            poppler = _poppler_path()

            expected_pages = convert_from_path(
                expected_pdf_path,
                dpi=dpi,
                fmt="png",
                poppler_path=poppler,
            )
            actual_pages = convert_from_path(
                original_pdf_path,
                dpi=dpi,
                fmt="png",
                poppler_path=poppler,
            )

            if not expected_pages and not actual_pages:
                return []

            base_name = Path(original_pdf_path).stem
            if result_id:
                base_name = f"{result_id}_{base_name}"

            fallback_size = (
                expected_pages[0].size if expected_pages
                else actual_pages[0].size
            )

            # ── Sequence alignment ──────────────────────────────────────────
            # Skip the DP when page counts are equal — direct 1-to-1 is enough
            # and saves thumbnail computation time.
            if len(expected_pages) == len(actual_pages):
                alignment = [
                    ("matched", i, i) for i in range(len(expected_pages))
                ]
                if len(expected_pages) == 0:
                    return []
            else:
                n_exp = len(expected_pages)
                n_act = len(actual_pages)
                logger.info(
                    "Page count mismatch: expected=%d actual=%d — running "
                    "sequence alignment DP",
                    n_exp, n_act,
                )
                alignment = self._align_pages(expected_pages, actual_pages)

            # ── Per-alignment-op comparison ─────────────────────────────────
            rows: List[Dict[str, Any]] = []

            for output_row_num, (op, exp_idx, act_idx) in enumerate(alignment, start=1):

                if op == "deleted":
                    # Page present in expected but absent in actual
                    exp = expected_pages[exp_idx].convert("RGB")
                    blank = self._create_blank_image(exp.size)
                    exp_n, blank_n = self._normalize_sizes(exp, blank)
                    empty_mask = Image.new("L", exp_n.size, 0)
                    panel = self._build_three_panel(exp_n, blank_n, empty_mask)
                    out_path = self.output_dir / f"{base_name}_page{output_row_num}.png"
                    panel.save(out_path, "PNG")
                    rows.append({
                        "page": output_row_num,
                        "expected_page_num": exp_idx + 1,
                        "actual_page_num": None,
                        "alignment_op": "deleted",
                        "similarity": 0.0,
                        "major": True,
                        "warn": False,
                        "note": (
                            f"Page {exp_idx + 1} of the expected PDF is absent in "
                            f"the actual PDF — this page was removed."
                        ),
                        "snapshot_path": f"visual_diffs/{out_path.name}",
                        "diff_bbox": None,
                        "diff_pixels_pct": 100.0,
                        "diff_area_pct": 100.0,
                        "signature_candidate": False,
                        "signature_label": None,
                        "signature_reason": "",
                        "signature_confidence": "none",
                    })
                    continue

                if op == "inserted":
                    # Page present in actual but absent in expected
                    act = actual_pages[act_idx].convert("RGB")
                    is_blank = self._is_blank_page(act)
                    blank = self._create_blank_image(act.size)
                    blank_n, act_n = self._normalize_sizes(blank, act)
                    empty_mask = Image.new("L", act_n.size, 0)
                    panel = self._build_three_panel(blank_n, act_n, empty_mask)
                    out_path = self.output_dir / f"{base_name}_page{output_row_num}.png"
                    panel.save(out_path, "PNG")
                    if is_blank:
                        inserted_note = (
                            f"Page {act_idx + 1} of the actual PDF is a blank page with no "
                            f"counterpart in the expected PDF — likely intentional (separator/placeholder). "
                            f"Validation continues from next page."
                        )
                    else:
                        inserted_note = (
                            f"Page {act_idx + 1} of the actual PDF has no counterpart "
                            f"in the expected PDF — this is an extra / inserted page."
                        )
                    rows.append({
                        "page": output_row_num,
                        "expected_page_num": None,
                        "actual_page_num": act_idx + 1,
                        "alignment_op": "inserted",
                        "is_blank_page": is_blank,
                        "similarity": 0.0,
                        "major": not is_blank,
                        "warn": is_blank,
                        "note": inserted_note,
                        "snapshot_path": f"visual_diffs/{out_path.name}",
                        "diff_bbox": None,
                        "diff_pixels_pct": 100.0,
                        "diff_area_pct": 100.0,
                        "signature_candidate": False,
                        "signature_label": None,
                        "signature_reason": "",
                        "signature_confidence": "none",
                    })
                    continue

                # op == "matched" — standard pixel-level comparison
                exp = expected_pages[exp_idx].convert("RGB")
                act = actual_pages[act_idx].convert("RGB")
                exp, act = self._normalize_sizes(exp, act)

                similarity, diff_pct, mask = self._compute_similarity_and_mask(act, exp)

                diff_bbox = self._get_diff_bbox(mask)
                diff_area_pct = self._bbox_area_pct(diff_bbox, exp.size) if diff_bbox else 0.0

                major = diff_pct >= 2.0
                warn = (diff_pct >= 0.10) and not major

                # Zone-aware semantic analysis
                zone_analysis = self._analyze_diff_zones(mask, diff_pct)
                diff_regions = self._extract_top_diff_regions(mask) if (major or warn) else []

                # Word-level text comparison (most reliable signal for real vs. noise)
                try:
                    text_diff = self._build_text_diff_annotations(
                        expected_pdf_path, original_pdf_path,
                        exp_idx + 1, act_idx + 1,
                        exp.size,
                    )
                except Exception as _te:
                    logger.warning("Text diff failed for page %d: %s", output_row_num, _te)
                    text_diff = {
                        "added_regions": [], "removed_regions": [],
                        "added_texts": [], "removed_texts": [],
                        "summary": "", "has_text_changes": False,
                    }

                # Compare PDF graphical elements (rects, lines, borders)
                try:
                    graphics_diff = self._compare_pdf_graphics(
                        expected_pdf_path, original_pdf_path,
                        exp_idx + 1, act_idx + 1,
                    )
                except Exception as _ge:
                    logger.warning("Graphics diff failed for page %d: %s", output_row_num, _ge)
                    graphics_diff = {"added_rects": 0, "removed_rects": 0, "has_graphics_changes": False, "summary": ""}

                has_graphics_changes = graphics_diff.get("has_graphics_changes", False)

                # Suppress noise ONLY when pixel diff is truly tiny (< 1%) —
                # real graphical changes (border boxes, annotations, decorations) still show up
                if (
                    not text_diff["has_text_changes"]
                    and not text_diff.get("has_formatting_changes")
                    and not has_graphics_changes
                    and zone_analysis.get("change_pattern") == "page_wide"
                    and diff_pct < 1.0  # sub-1% = pure DPI/font-hinting noise
                ):
                    major = False
                    warn = False
                elif (
                    not text_diff["has_text_changes"]
                    and not text_diff.get("has_formatting_changes")
                    and not has_graphics_changes
                    and zone_analysis.get("change_pattern") == "page_wide"
                    and diff_pct < 5.0  # 1-5% with no text/fmt/graphics change = rendering noise
                ):
                    major = False
                    # keep warn so tester still sees a low-level notice

                note = self._generate_semantic_note(diff_pct, zone_analysis, major, warn)
                if text_diff["summary"]:
                    note = f"{note} TEXT: {text_diff['summary']}"
                if graphics_diff.get("summary"):
                    note = f"{note} GRAPHICS: {graphics_diff['summary']}"

                # Annotate when pages were re-ordered / shifted
                if exp_idx != act_idx:
                    note += (
                        f" (aligned: expected p{exp_idx + 1} "
                        f"→ actual p{act_idx + 1})"
                    )

                signature_candidate = False
                signature_label = None
                signature_reason = ""
                signature_confidence = "none"

                if diff_bbox:
                    signature_labels = self._find_signature_labels(
                        expected_pdf_path, exp_idx + 1, exp.size
                    )
                    signature_eval = self._evaluate_signature_candidate(
                        diff_bbox=diff_bbox,
                        image_size=exp.size,
                        labels=signature_labels,
                        diff_pixels_pct=diff_pct,
                        diff_area_pct=diff_area_pct,
                    )
                    signature_candidate = signature_eval["candidate"]
                    signature_label = signature_eval["label"]
                    signature_reason = signature_eval["reason"]
                    signature_confidence = signature_eval["confidence"]

                    if signature_candidate:
                        warn = True
                        if signature_confidence == "high":
                            note = "Possible missing or changed signature detected."
                        else:
                            note = "Possible signature-region change detected."

                # ── Annotate EXPECTED panel ──────────────────────────────────
                # Line-level removed: thick 3px red border around the whole line
                # Word-level removed: thinner 2px red border around the word
                exp_annotated = exp.copy()
                draw_e = ImageDraw.Draw(exp_annotated)
                font_e = self._get_small_font(10)
                for region in text_diff["removed_regions"][:50]:
                    x0, y0, x1, y1 = region["bbox"]
                    bw = 3 if region.get("line_level") else 2
                    pad = 2 if region.get("line_level") else 1
                    draw_e.rectangle(
                        [max(0, x0 - pad), max(0, y0 - pad),
                         min(exp.width - 1, x1 + pad), min(exp.height - 1, y1 + pad)],
                        outline=(220, 40, 40), width=bw,
                    )

                # ── Annotate ACTUAL panel ────────────────────────────────────
                # Line-level added: thick 3px green border
                # Word-level added: thinner 2px green border
                # Orange = bold changed  |  Yellow = font size changed  |  Blue = alignment shifted
                act_annotated = act.copy()
                draw_a = ImageDraw.Draw(act_annotated)
                font_a = self._get_small_font(10)

                for region in text_diff["added_regions"][:50]:
                    x0, y0, x1, y1 = region["bbox"]
                    bw = 3 if region.get("line_level") else 2
                    pad = 2 if region.get("line_level") else 1
                    draw_a.rectangle(
                        [max(0, x0 - pad), max(0, y0 - pad),
                         min(act.width - 1, x1 + pad), min(act.height - 1, y1 + pad)],
                        outline=(40, 200, 70), width=bw,
                    )

                for region in text_diff.get("bold_changed_regions", [])[:30]:
                    x0, y0, x1, y1 = region["bbox_act"]
                    draw_a.rectangle(
                        [max(0, x0 - 2), max(0, y0 - 2),
                         min(act.width - 1, x1 + 2), min(act.height - 1, y1 + 2)],
                        outline=(255, 140, 0), width=2,   # orange — bold changed
                    )

                for region in text_diff.get("size_changed_regions", [])[:20]:
                    x0, y0, x1, y1 = region["bbox_act"]
                    draw_a.rectangle(
                        [max(0, x0 - 2), max(0, y0 - 2),
                         min(act.width - 1, x1 + 2), min(act.height - 1, y1 + 2)],
                        outline=(255, 210, 0), width=2,   # yellow — font size changed
                    )

                for region in text_diff.get("alignment_shifted_regions", [])[:30]:
                    x0, y0, x1, y1 = region["bbox_act"]
                    # List markers get a thicker border to call attention
                    border_w = 3 if region.get("is_list_marker") else 2
                    draw_a.rectangle(
                        [max(0, x0 - 2), max(0, y0 - 2),
                         min(act.width - 1, x1 + 2), min(act.height - 1, y1 + 2)],
                        outline=(80, 160, 255), width=border_w,  # blue — alignment shifted
                    )

                # Diff count summary for header
                diff_counts = {
                    "added": len(text_diff["added_texts"]),
                    "removed": len(text_diff["removed_texts"]),
                    "bold": len(text_diff.get("bold_changed_regions", [])),
                    "size": len(text_diff.get("size_changed_regions", [])),
                    "align": len(text_diff.get("alignment_shifted_regions", [])),
                }

                panel = self._build_three_panel(
                    exp_annotated, act_annotated, mask,
                    diff_regions, zone_analysis, signature_candidate,
                    diff_counts=diff_counts,
                )
                out_path = self.output_dir / f"{base_name}_page{output_row_num}.png"
                panel.save(out_path, "PNG")

                rows.append({
                    "page": output_row_num,
                    "expected_page_num": exp_idx + 1,
                    "actual_page_num": act_idx + 1,
                    "alignment_op": "matched",
                    "similarity": round(similarity, 3),
                    "major": bool(major),
                    "warn": bool(warn),
                    "note": note,
                    "snapshot_path": f"visual_diffs/{out_path.name}",
                    "diff_bbox": list(diff_bbox) if diff_bbox else None,
                    "diff_pixels_pct": round(diff_pct, 4),
                    "diff_area_pct": round(diff_area_pct, 4),
                    "signature_candidate": signature_candidate,
                    "signature_label": signature_label,
                    "signature_reason": signature_reason,
                    "signature_confidence": signature_confidence,
                    "zone_analysis": zone_analysis,
                    "diff_regions": diff_regions,
                    "text_diff_summary": text_diff["summary"],
                    "added_texts": text_diff["added_texts"],
                    "removed_texts": text_diff["removed_texts"],
                    "has_text_changes": text_diff["has_text_changes"],
                    "has_formatting_changes": text_diff.get("has_formatting_changes", False),
                    "formatting_summary": text_diff.get("formatting_summary", ""),
                    "bold_changed": [r["text"] for r in text_diff.get("bold_changed_regions", [])],
                    "size_changed": [
                        f"{r['text']} ({r['exp_size']}pt→{r['act_size']}pt)"
                        for r in text_diff.get("size_changed_regions", [])
                    ],
                    "list_alignment_shifted": [
                        r["text"] for r in text_diff.get("alignment_shifted_regions", [])
                        if r.get("is_list_marker")
                    ],
                    "graphics_diff": graphics_diff.get("summary", ""),
                    "has_graphics_changes": has_graphics_changes,
                })

            return rows

        except Exception as e:
            logger.error(f"Error comparing PDFs (detailed): {str(e)}", exc_info=True)
            return []

    def compare_pdfs(self, original_pdf_path: str, expected_pdf_path: str, result_id: Optional[str] = None) -> List[str]:
        rows = self.compare_pdfs_detailed(original_pdf_path, expected_pdf_path, result_id=result_id)
        return [r.get("snapshot_path") for r in rows if r.get("snapshot_path")]

    # ---------------------------------------------------
    # Page sequence alignment
    # ---------------------------------------------------

    _THUMB_SIZE: Tuple[int, int] = (150, 210)   # ~13 DPI equivalent — fast but discriminative

    def _align_pages(
        self,
        expected_pages: List[Image.Image],
        actual_pages: List[Image.Image],
    ) -> List[Tuple[str, Optional[int], Optional[int]]]:
        """
        Compute the minimum-cost alignment between two page sequences using
        edit-distance dynamic programming (analogous to the Myers diff algorithm
        but operating on rendered page images).

        Operations and costs
        --------------------
        matched : _ALIGN_MATCH_SCALE * (1 - sim)
            Aligning expected[i] with actual[j].  Cost is zero when the pages
            are identical, and rises to _ALIGN_MATCH_SCALE when they share
            nothing.  A match is preferred over delete+insert when the two pages
            are more than 50 % visually similar.
        deleted : _ALIGN_DELETE_COST
            Expected[i] has no counterpart in actual (page was removed).
        inserted : _ALIGN_INSERT_COST
            Actual[j] has no counterpart in expected (page was added).

        Returns a list of (op, exp_idx, act_idx) tuples in document order.
        """
        n = len(expected_pages)
        m = len(actual_pages)

        # Edge cases
        if n == 0:
            return [("inserted", None, j) for j in range(m)]
        if m == 0:
            return [("deleted", i, None) for i in range(n)]

        # ── Build thumbnail similarity matrix ───────────────────────────────
        # Thumbnails are computed once and reused for all DP cell evaluations.
        def _thumb(img: Image.Image) -> Image.Image:
            t = img.convert("L").resize(self._THUMB_SIZE, Image.LANCZOS)
            return t.convert("RGB")

        exp_thumbs = [_thumb(p) for p in expected_pages]
        act_thumbs = [_thumb(p) for p in actual_pages]

        sim: List[List[float]] = []
        for i in range(n):
            row: List[float] = []
            for j in range(m):
                et, at = self._normalize_sizes(exp_thumbs[i], act_thumbs[j])
                s, _, _ = self._compute_similarity_and_mask(at, et)
                row.append(s)
            sim.append(row)

        logger.debug("Alignment similarity matrix %dx%d computed", n, m)

        # ── Edit-distance DP ────────────────────────────────────────────────
        INF = float("inf")

        # dp[i][j] = min cost to align first i expected pages with first j actual pages
        dp: List[List[float]] = [[INF] * (m + 1) for _ in range(n + 1)]
        dp[0][0] = 0.0
        for i in range(1, n + 1):
            dp[i][0] = i * _ALIGN_DELETE_COST
        for j in range(1, m + 1):
            dp[0][j] = j * _ALIGN_INSERT_COST

        for i in range(1, n + 1):
            for j in range(1, m + 1):
                cost_match  = dp[i - 1][j - 1] + _ALIGN_MATCH_SCALE * (1.0 - sim[i - 1][j - 1])
                cost_delete = dp[i - 1][j]     + _ALIGN_DELETE_COST
                cost_insert = dp[i][j - 1]     + _ALIGN_INSERT_COST
                dp[i][j] = min(cost_match, cost_delete, cost_insert)

        # ── Traceback ───────────────────────────────────────────────────────
        ops: List[Tuple[str, Optional[int], Optional[int]]] = []
        i, j = n, m
        while i > 0 or j > 0:
            if i > 0 and j > 0:
                cost_match  = dp[i - 1][j - 1] + _ALIGN_MATCH_SCALE * (1.0 - sim[i - 1][j - 1])
                cost_delete = dp[i - 1][j]     + _ALIGN_DELETE_COST
                cost_insert = dp[i][j - 1]     + _ALIGN_INSERT_COST
                best = min(cost_match, cost_delete, cost_insert)

                if abs(dp[i][j] - cost_match) < 1e-9:
                    ops.append(("matched", i - 1, j - 1))
                    i -= 1; j -= 1
                elif abs(dp[i][j] - cost_delete) < 1e-9:
                    ops.append(("deleted", i - 1, None))
                    i -= 1
                else:
                    ops.append(("inserted", None, j - 1))
                    j -= 1
            elif i > 0:
                ops.append(("deleted", i - 1, None))
                i -= 1
            else:
                ops.append(("inserted", None, j - 1))
                j -= 1

        ops.reverse()

        # Log a compact summary for debugging
        summary = {"matched": 0, "deleted": 0, "inserted": 0}
        for op, _, _ in ops:
            summary[op] += 1
        logger.info(
            "Alignment result: %d matched, %d deleted, %d inserted",
            summary["matched"], summary["deleted"], summary["inserted"],
        )

        return ops

    # ---------------------------------------------------
    # Internals
    # ---------------------------------------------------
    def _is_blank_page(self, img: Image.Image, white_threshold: int = 250, blank_pct: float = 98.0) -> bool:
        """Return True when ≥ blank_pct % of pixels are near-white (blank / near-blank page)."""
        try:
            gray = img.convert("L")
            pixels = list(gray.getdata())
            if not pixels:
                return False
            near_white = sum(1 for p in pixels if p >= white_threshold)
            return (near_white / len(pixels)) * 100.0 >= blank_pct
        except Exception:
            return False

    def _create_blank_image(self, size: Optional[Tuple[int, int]]) -> Image.Image:
        if not size:
            size = (1000, 1400)
        img = Image.new("RGB", size, (255, 255, 255))
        draw = ImageDraw.Draw(img)
        draw.text((20, 20), "Blank Page", fill=(80, 80, 80))
        return img

    def _normalize_sizes(self, a: Image.Image, b: Image.Image) -> Tuple[Image.Image, Image.Image]:
        max_w = max(a.width, b.width)
        max_h = max(a.height, b.height)
        if a.size != (max_w, max_h):
            a = self._pad_to_size(a, (max_w, max_h))
        if b.size != (max_w, max_h):
            b = self._pad_to_size(b, (max_w, max_h))
        return a, b

    def _pad_to_size(self, img: Image.Image, target: Tuple[int, int]) -> Image.Image:
        bg = Image.new("RGB", target, (255, 255, 255))
        bg.paste(img, (0, 0))
        return bg

    def _compute_similarity_and_mask(self, actual: Image.Image, expected: Image.Image) -> Tuple[float, float, Image.Image]:
        diff = ImageChops.difference(actual, expected).convert("L")

        # Threshold 25 filters anti-aliasing/JPEG noise while still catching real changes
        threshold = 25
        mask = diff.point(lambda x: 255 if x > threshold else 0)

        hist = mask.histogram()           # 256 buckets; [0] = unchanged pixels
        diff_pixels = sum(hist[1:])
        total_pixels = actual.width * actual.height if actual.width and actual.height else 0

        diff_pct = (diff_pixels / total_pixels) * 100.0 if total_pixels else 0.0
        similarity = max(0.0, min(1.0, (100.0 - diff_pct) / 100.0))

        return similarity, diff_pct, mask

    def _get_diff_bbox(self, mask: Image.Image) -> Optional[Tuple[int, int, int, int]]:
        return mask.getbbox()

    def _bbox_area_pct(self, bbox: Optional[Tuple[int, int, int, int]], image_size: Tuple[int, int]) -> float:
        if not bbox:
            return 0.0
        x0, y0, x1, y1 = bbox
        area = max(0, x1 - x0) * max(0, y1 - y0)
        total = max(1, image_size[0] * image_size[1])
        return (area / total) * 100.0

    def _boxes_near(
        self,
        box1: Tuple[int, int, int, int],
        box2: Tuple[float, float, float, float],
        x_pad: int = 120,
        y_pad: int = 80,
    ) -> bool:
        a_x0, a_y0, a_x1, a_y1 = box1
        b_x0, b_y0, b_x1, b_y1 = box2

        return not (
            a_x1 < (b_x0 - x_pad)
            or a_x0 > (b_x1 + x_pad)
            or a_y1 < (b_y0 - y_pad)
            or a_y0 > (b_y1 + y_pad)
        )

    def _is_signature_zone(
        self,
        bbox: Tuple[int, int, int, int],
        image_size: Tuple[int, int],
    ) -> bool:
        """
        Signature zones are usually in the lower half / lower third of the page.
        Tightened to reduce false positives.
        """
        x0, y0, x1, y1 = bbox
        _w, h = image_size
        center_y = (y0 + y1) / 2.0
        height = max(1, y1 - y0)

        # Lower 45% of page and not too tall
        return center_y >= (h * 0.52) and height <= (h * 0.16)

    def _find_signature_labels(
        self,
        pdf_path: str,
        page_number: int,
        image_size: Tuple[int, int],
    ) -> List[Dict[str, Any]]:
        """
        Returns signature-related labels with their bounding boxes scaled
        from PDF coordinates to rendered image coordinates.
        """
        labels: List[Dict[str, Any]] = []
        try:
            with pdfplumber.open(pdf_path) as pdf:
                if not (1 <= page_number <= len(pdf.pages)):
                    return []

                page = pdf.pages[page_number - 1]
                words = page.extract_words() or []
                if not words:
                    return []

                pdf_w = float(page.width or 1.0)
                pdf_h = float(page.height or 1.0)
                img_w, img_h = image_size

                sx = img_w / pdf_w
                sy = img_h / pdf_h

                for w in words:
                    txt = str(w.get("text", "")).upper().strip()
                    if txt in self.SIGNATURE_KEYWORDS:
                        labels.append(
                            {
                                "text": txt,
                                "bbox": (
                                    float(w["x0"]) * sx,
                                    float(w["top"]) * sy,
                                    float(w["x1"]) * sx,
                                    float(w["bottom"]) * sy,
                                ),
                            }
                        )
        except Exception:
            return []

        return labels

    def _evaluate_signature_candidate(
        self,
        diff_bbox: Tuple[int, int, int, int],
        image_size: Tuple[int, int],
        labels: List[Dict[str, Any]],
        diff_pixels_pct: float,
        diff_area_pct: float,
    ) -> Dict[str, Any]:
        """
        Precision-first signature evaluation.
        Conditions for a credible signature candidate:
        - diff must be inside signature zone
        - diff must be near a strict signature label
        - diff must be compact (not huge page-level block)
        - diff must not be too tiny to be noise
        """
        if not diff_bbox or not labels:
            return {
                "candidate": False,
                "label": None,
                "reason": "",
                "confidence": "none",
            }

        if not self._is_signature_zone(diff_bbox, image_size):
            return {
                "candidate": False,
                "label": None,
                "reason": "",
                "confidence": "none",
            }

        # compact / ink-like heuristic
        # Too large means probably page-wide layout drift, not signature ink
        if diff_area_pct > 3.0:
            return {
                "candidate": False,
                "label": None,
                "reason": "",
                "confidence": "none",
            }

        # Too tiny can be dust/noise
        if diff_pixels_pct < 0.01:
            return {
                "candidate": False,
                "label": None,
                "reason": "",
                "confidence": "none",
            }

        best_label = None
        for lbl in labels:
            if self._boxes_near(diff_bbox, lbl["bbox"], x_pad=120, y_pad=70):
                best_label = lbl
                break

        if not best_label:
            return {
                "candidate": False,
                "label": None,
                "reason": "",
                "confidence": "none",
            }

        label_text = best_label["text"]

        # Confidence tuning:
        # PRESIDENT/SECRETARY are stronger than generic SIGNATURE
        if label_text in {"PRESIDENT", "SECRETARY"} and diff_area_pct <= 1.0:
            confidence = "high"
        else:
            confidence = "medium"

        return {
            "candidate": True,
            "label": label_text,
            "reason": f"Changed region is in a signature zone near label '{label_text}'.",
            "confidence": confidence,
        }

    def _analyze_diff_zones(
        self,
        mask: Image.Image,
        diff_pct: float,
    ) -> Dict[str, Any]:
        """Analyze which page zones contain differences and classify the change pattern."""
        w, h = mask.size
        zone_bounds = {
            "header":      (0, 0,            w, int(h * 0.12)),
            "upper_body":  (0, int(h * 0.12), w, int(h * 0.42)),
            "middle_body": (0, int(h * 0.42), w, int(h * 0.72)),
            "lower_body":  (0, int(h * 0.72), w, int(h * 0.87)),
            "footer":      (0, int(h * 0.87), w, h),
        }
        zone_pcts: Dict[str, float] = {}
        for name, bbox in zone_bounds.items():
            cropped = mask.crop(bbox)
            pixels = list(cropped.getdata())
            if pixels:
                diff = sum(1 for p in pixels if p > 0)
                zone_pcts[name] = round((diff / len(pixels)) * 100.0, 2)
            else:
                zone_pcts[name] = 0.0

        SIG = 1.5
        changed_zones = [name for name, pct in zone_pcts.items() if pct > SIG]

        if not changed_zones:
            pattern = "no_change"
            hint = "No significant visual differences detected."
        elif len(changed_zones) >= 4:
            pattern = "page_wide"
            hint = (
                "Changes are spread across the entire page. "
                "This is likely a rendering, font, watermark, or DPI difference "
                "rather than specific content changes."
            )
        elif changed_zones == ["header"]:
            pattern = "header_only"
            hint = "Change confined to page header — check policy number, date, logo, or named insured field."
        elif set(changed_zones) <= {"header", "upper_body"}:
            pattern = "header_and_fields"
            hint = "Changes in header and upper body — likely field values were populated or modified."
        elif set(changed_zones) <= {"upper_body", "middle_body", "lower_body"}:
            pattern = "body_content"
            hint = "Changes in the main body — check for text updates, inserted/deleted clauses, or field values."
        elif "lower_body" in changed_zones or "footer" in changed_zones:
            pattern = "footer_area"
            hint = "Changes near the footer/signature area — check signature blocks, dates, and footer text."
        else:
            pattern = "partial"
            hint = f"Changes detected in: {', '.join(changed_zones)}."

        return {
            "zone_diff_pcts": zone_pcts,
            "changed_zones": changed_zones,
            "change_pattern": pattern,
            "change_hint": hint,
        }

    def _extract_top_diff_regions(
        self,
        mask: Image.Image,
        grid_cols: int = 8,
        grid_rows: int = 14,
        min_cell_diff_pct: float = 8.0,
        max_regions: int = 8,
    ) -> List[Tuple[int, int, int, int]]:
        """Extract bounding boxes of the most significant diff regions via grid sampling."""
        w, h = mask.size
        cell_w = max(1, w // grid_cols)
        cell_h = max(1, h // grid_rows)

        hot_cells: List[Tuple[int, int, int, int]] = []
        for row in range(grid_rows):
            for col in range(grid_cols):
                x0 = col * cell_w
                y0 = row * cell_h
                x1 = min(x0 + cell_w, w)
                y1 = min(y0 + cell_h, h)
                cell = mask.crop((x0, y0, x1, y1))
                pixels = list(cell.getdata())
                if not pixels:
                    continue
                diff_count = sum(1 for p in pixels if p > 0)
                if (diff_count / len(pixels)) * 100.0 >= min_cell_diff_pct:
                    hot_cells.append((x0, y0, x1, y1))

        if not hot_cells:
            return []

        merged = self._merge_boxes(hot_cells, padding=cell_w)
        merged.sort(key=lambda b: (b[2] - b[0]) * (b[3] - b[1]), reverse=True)
        return merged[:max_regions]

    def _merge_boxes(
        self,
        boxes: List[Tuple[int, int, int, int]],
        padding: int = 5,
    ) -> List[Tuple[int, int, int, int]]:
        """Merge overlapping or adjacent bounding boxes."""
        if not boxes:
            return []
        changed = True
        result = list(boxes)
        while changed:
            changed = False
            new_result: List[Tuple[int, int, int, int]] = []
            used = [False] * len(result)
            for i in range(len(result)):
                if used[i]:
                    continue
                x0, y0, x1, y1 = result[i]
                for j in range(i + 1, len(result)):
                    if used[j]:
                        continue
                    bx0, by0, bx1, by1 = result[j]
                    if not (x1 + padding < bx0 or bx1 + padding < x0 or
                            y1 + padding < by0 or by1 + padding < y0):
                        x0 = min(x0, bx0)
                        y0 = min(y0, by0)
                        x1 = max(x1, bx1)
                        y1 = max(y1, by1)
                        used[j] = True
                        changed = True
                new_result.append((x0, y0, x1, y1))
            result = new_result
        return result

    def _generate_semantic_note(
        self,
        diff_pct: float,
        zone_analysis: Dict[str, Any],
        major: bool,
        warn: bool,
    ) -> str:
        """Generate a human-readable description of the visual difference."""
        if not major and not warn:
            return "No material visual differences detected."
        hint = zone_analysis.get("change_hint", "")
        if major:
            base = f"Significant visual differences detected ({diff_pct:.1f}% of pixels differ)."
        else:
            base = f"Minor visual differences detected ({diff_pct:.2f}% of pixels differ)."
        return f"{base} {hint}".strip() if hint else base

    # Words that are almost certainly watermark/stamp artifacts, not real content
    _WATERMARK_WORDS: frozenset = frozenset({
        "SPECIMEN", "SAMPLE", "DRAFT", "VOID", "COPY",
        "CONFIDENTIAL", "DUPLICATE", "CANCELLED", "CANCELED", "SUPERSEDED",
    })

    def _compare_pdf_graphics(
        self,
        expected_pdf_path: str,
        actual_pdf_path: str,
        expected_page_num: int,
        actual_page_num: int,
    ) -> Dict[str, Any]:
        """
        Compare PDF graphical elements (rectangles, lines, curves) between two pages.

        This catches visual differences that are invisible to word extraction:
        - Decorative border boxes drawn around text
        - Underlines / strikethroughs implemented as drawn lines
        - Shaded / highlighted regions
        - Added or removed divider lines

        Returns counts of added/removed rects and a human-readable summary.
        """
        _SNAP = 5.0   # points — positions within this distance are treated as the same

        def _extract_rects(pdf_path: str, page_num: int) -> List[Tuple[float, float, float, float]]:
            try:
                with pdfplumber.open(pdf_path) as pdf:
                    if not (1 <= page_num <= len(pdf.pages)):
                        return []
                    page = pdf.pages[page_num - 1]
                    out: List[Tuple[float, float, float, float]] = []
                    for r in (page.rects or []):
                        try:
                            out.append((
                                round(float(r.get("x0", 0)), 1),
                                round(float(r.get("top", 0)), 1),
                                round(float(r.get("x1", 0)), 1),
                                round(float(r.get("bottom", 0)), 1),
                            ))
                        except Exception:
                            pass
                    for ln in (page.lines or []):
                        try:
                            out.append((
                                round(float(ln.get("x0", 0)), 1),
                                round(float(ln.get("top", 0)), 1),
                                round(float(ln.get("x1", 0)), 1),
                                round(float(ln.get("bottom", 0)), 1),
                            ))
                        except Exception:
                            pass
                    return out
            except Exception:
                return []

        def _find_unmatched(set_a, set_b):
            """Return items in set_a that have no near-match in set_b."""
            unmatched = []
            for a in set_a:
                matched = any(
                    abs(a[0] - b[0]) <= _SNAP and abs(a[1] - b[1]) <= _SNAP
                    and abs(a[2] - b[2]) <= _SNAP and abs(a[3] - b[3]) <= _SNAP
                    for b in set_b
                )
                if not matched:
                    unmatched.append(a)
            return unmatched

        exp_rects = _extract_rects(expected_pdf_path, expected_page_num)
        act_rects = _extract_rects(actual_pdf_path, actual_page_num)

        removed_rects = _find_unmatched(exp_rects, act_rects)  # in expected, not in actual
        added_rects   = _find_unmatched(act_rects, exp_rects)  # in actual, not in expected

        has_graphics_changes = bool(removed_rects or added_rects)
        parts = []
        if removed_rects:
            parts.append(f"{len(removed_rects)} border/line element(s) removed from current PDF")
        if added_rects:
            parts.append(f"{len(added_rects)} border/line element(s) added in current PDF")

        return {
            "added_rects": len(added_rects),
            "removed_rects": len(removed_rects),
            "has_graphics_changes": has_graphics_changes,
            "summary": " | ".join(parts),
        }

    # List/bullet marker pattern — these have tighter alignment tolerance
    _LIST_MARKER_RE = re.compile(
        r'^(\([a-zA-Z0-9]\)|[a-zA-Z]\.|[0-9]+\.|'
        r'\([ivxIVX]+\)|[ivxIVX]+\.|[\u2022\u2023\u25e6\u2043\u2219•\-–])$'
    )

    @staticmethod
    def _is_list_marker(text: str) -> bool:
        return bool(VisualDiff._LIST_MARKER_RE.match(text.strip()))

    def _build_text_diff_annotations(
        self,
        expected_pdf_path: str,
        actual_pdf_path: str,
        expected_page_num: int,
        actual_page_num: int,
        image_size: Tuple[int, int],
    ) -> Dict[str, Any]:
        """
        Paragraph-level diff engine — clean, precise visual annotations.

        KEY INSIGHT: PDF line breaks are visual rendering artifacts, NOT semantic
        boundaries.  The same sentence may wrap at different positions in two
        renders of nearly identical documents.  Diffing at line level treats these
        reflow differences as content changes and floods every panel with boxes.

        Algorithm:
        1. Extract words → cluster into text lines (4pt Y-band).
        2. Cluster lines into paragraphs (gap > 0.7 × line-height = new paragraph).
        3. Run SequenceMatcher diff on paragraph texts — reflow-resistant because
           the full paragraph text matches even when internal line breaks differ.
        4. For changed paragraphs (1-to-1 replace): word-level diff to box only
           the exact changed words.  Small, precise boxes.
        5. For deleted/inserted whole paragraphs: one thin rectangle around the
           paragraph.
        6. For equal paragraphs: detect bold/size/alignment changes.
        7. Cap annotation boxes at 20 per panel to prevent visual flooding.
        """
        import difflib

        _Y_LINE_TOL  = 4.0   # points — Y-band for line clustering
        _PARA_GAP    = 0.7   # gap > this × line_height = paragraph break
        _MAX_BOXES   = 20    # max boxes drawn per panel

        _ALIGN_TOLERANCE_BODY = 15.0
        _ALIGN_TOLERANCE_LIST = 5.0
        _MIN_ALIGN_WORDS_BODY = 3
        _SIZE_TOLERANCE = 1.2

        # ── Helpers ──────────────────────────────────────────────────────────
        def _extract_lines(pdf_path: str, page_num: int):
            try:
                with pdfplumber.open(pdf_path) as pdf:
                    if not (1 <= page_num <= len(pdf.pages)):
                        return [], 612.0, 792.0
                    page = pdf.pages[page_num - 1]
                    try:
                        words = page.extract_words(
                            extra_attrs=["fontname", "size"],
                            x_tolerance=3, y_tolerance=3
                        ) or []
                    except Exception:
                        words = page.extract_words() or []
                    pw = float(page.width or 612.0)
                    ph = float(page.height or 792.0)
            except Exception:
                return [], 612.0, 792.0

            words = [
                w for w in words
                if len(w.get("text", "").strip()) >= 1
                and w.get("text", "").strip().upper() not in self._WATERMARK_WORDS
            ]
            if not words:
                return [], pw, ph

            words = sorted(words, key=lambda w: float(w.get("top", 0)))
            lines: List[List[dict]] = []
            cur: List[dict] = [words[0]]
            cur_y = float(words[0].get("top", 0))
            for w in words[1:]:
                wy = float(w.get("top", 0))
                if abs(wy - cur_y) <= _Y_LINE_TOL:
                    cur.append(w)
                else:
                    cur.sort(key=lambda x: float(x.get("x0", 0)))
                    lines.append(cur)
                    cur = [w]
                    cur_y = wy
            if cur:
                cur.sort(key=lambda x: float(x.get("x0", 0)))
                lines.append(cur)
            return lines, pw, ph

        def _cluster_paragraphs(lines):
            """Group lines into paragraphs by detecting larger vertical gaps."""
            if not lines:
                return []
            paras = [[lines[0]]]
            for i in range(1, len(lines)):
                prev, curr = lines[i - 1], lines[i]
                prev_bottom = max(float(w.get("bottom", 0)) for w in prev)
                curr_top    = min(float(w.get("top",    0)) for w in curr)
                h = max((float(w.get("bottom", 0)) - float(w.get("top", 0)) for w in curr), default=10.0)
                if (curr_top - prev_bottom) > h * _PARA_GAP:
                    paras.append([curr])
                else:
                    paras[-1].append(curr)
            return paras

        def _para_text(para):
            return " ".join(w.get("text", "") for line in para for w in line)

        def _para_words(para):
            return [w for line in para for w in line]

        def _para_bbox(para, sx, sy):
            ws = _para_words(para)
            return (
                int(min(float(w.get("x0",     0)) for w in ws) * sx),
                int(min(float(w.get("top",    0)) for w in ws) * sy),
                int(max(float(w.get("x1",     0)) for w in ws) * sx),
                int(max(float(w.get("bottom", 0)) for w in ws) * sy),
            )

        def _scale_word(w, sx, sy):
            return (
                int(float(w.get("x0",     0)) * sx),
                int(float(w.get("top",    0)) * sy),
                int(float(w.get("x1",     0)) * sx),
                int(float(w.get("bottom", 0)) * sy),
            )

        def _is_bold(w):
            f = str(w.get("fontname", "") or "").lower()
            return (
                "bold" in f or "heavy" in f or "black" in f
                or "demibold" in f or "semibold" in f
                or ",b" in f or "-bd" in f or "-b," in f
                or f.endswith(",b") or f.endswith("-b")
                or f.endswith("bold") or f.endswith("heavy")
            )

        # ── Extract + cluster ────────────────────────────────────────────────
        img_w, img_h = image_size
        exp_lines, exp_pw, exp_ph = _extract_lines(expected_pdf_path, expected_page_num)
        act_lines, act_pw, act_ph = _extract_lines(actual_pdf_path,   actual_page_num)
        exp_sx, exp_sy = img_w / exp_pw, img_h / exp_ph
        act_sx, act_sy = img_w / act_pw, img_h / act_ph

        exp_paras = _cluster_paragraphs(exp_lines)
        act_paras = _cluster_paragraphs(act_lines)

        exp_ptexts = [_para_text(p) for p in exp_paras]
        act_ptexts = [_para_text(p) for p in act_paras]

        # ── Paragraph-level diff ─────────────────────────────────────────────
        matcher  = difflib.SequenceMatcher(None, exp_ptexts, act_ptexts, autojunk=False)
        opcodes  = matcher.get_opcodes()

        changes:         List[Dict] = []
        added_regions:   List[Dict] = []
        removed_regions: List[Dict] = []
        raw_added:   List[str] = []
        raw_removed: List[str] = []

        for tag, i1, i2, j1, j2 in opcodes:
            if tag == "equal":
                continue
            exp_chunk = exp_paras[i1:i2]
            act_chunk = act_paras[j1:j2]

            if tag == "delete":
                for para in exp_chunk:
                    txt = _para_text(para)
                    removed_regions.append({"text": txt, "bbox": _para_bbox(para, exp_sx, exp_sy), "line_level": True})
                    raw_removed.append(txt)
                changes.append({"type": "deleted",
                                 "exp_text": "\n".join(_para_text(p) for p in exp_chunk), "act_text": ""})

            elif tag == "insert":
                for para in act_chunk:
                    txt = _para_text(para)
                    added_regions.append({"text": txt, "bbox": _para_bbox(para, act_sx, act_sy), "line_level": True})
                    raw_added.append(txt)
                changes.append({"type": "inserted",
                                 "exp_text": "", "act_text": "\n".join(_para_text(p) for p in act_chunk)})

            elif tag == "replace":
                if len(exp_chunk) == len(act_chunk) == 1:
                    # 1-to-1 paragraph: word-level diff for precise boxes
                    ep = exp_chunk[0]; ap = act_chunk[0]
                    ews = _para_words(ep); aws = _para_words(ap)
                    et  = [w.get("text", "") for w in ews]
                    at  = [w.get("text", "") for w in aws]
                    wm  = difflib.SequenceMatcher(None, et, at, autojunk=False)
                    word_changed = False
                    for wtag, wi1, wi2, wj1, wj2 in wm.get_opcodes():
                        if wtag == "equal":
                            continue
                        word_changed = True
                        if wtag in ("delete", "replace"):
                            for w in ews[wi1:wi2]:
                                removed_regions.append({"text": w.get("text", ""),
                                                         "bbox": _scale_word(w, exp_sx, exp_sy),
                                                         "line_level": False})
                                raw_removed.append(w.get("text", ""))
                        if wtag in ("insert", "replace"):
                            for w in aws[wj1:wj2]:
                                added_regions.append({"text": w.get("text", ""),
                                                       "bbox": _scale_word(w, act_sx, act_sy),
                                                       "line_level": False})
                                raw_added.append(w.get("text", ""))
                    if word_changed:
                        changes.append({"type": "modified",
                                         "exp_text": _para_text(ep), "act_text": _para_text(ap)})
                else:
                    # Multi-para replacement: one rectangle per paragraph
                    for para in exp_chunk:
                        txt = _para_text(para)
                        removed_regions.append({"text": txt, "bbox": _para_bbox(para, exp_sx, exp_sy), "line_level": True})
                        raw_removed.append(txt)
                    for para in act_chunk:
                        txt = _para_text(para)
                        added_regions.append({"text": txt, "bbox": _para_bbox(para, act_sx, act_sy), "line_level": True})
                        raw_added.append(txt)
                    changes.append({"type": "replaced_block",
                                     "exp_text": "\n".join(_para_text(p) for p in exp_chunk)[:200],
                                     "act_text": "\n".join(_para_text(p) for p in act_chunk)[:200]})

        # Cap boxes to prevent flooding
        added_regions   = added_regions[:_MAX_BOXES]
        removed_regions = removed_regions[:_MAX_BOXES]

        # ── Formatting diff on EQUAL paragraphs and 1-to-1 replacements ────────
        # Covers:
        #   equal  → same text, may differ in bold/size/alignment
        #   replace 1:1 → text changed but same paragraph structure; bold may
        #                 have changed simultaneously (e.g. heading became bold
        #                 while its wording was also updated)
        bold_changed_regions:   List[Dict] = []
        size_changed_regions:   List[Dict] = []
        alignment_shifted_list: List[Dict] = []
        alignment_shifted_body: List[Dict] = []

        def _check_paragraph_formatting(ep, ap):
            """Compare word-level formatting between two corresponding paragraphs."""
            exp_llines = [sorted(line, key=lambda w: float(w.get("x0", 0))) for line in ep]
            act_llines = [sorted(line, key=lambda w: float(w.get("x0", 0))) for line in ap]
            for el, al in zip(exp_llines, act_llines):
                # Position-based matching handles duplicate words correctly
                for ew, aw in zip(el, al):
                    exp_t = ew.get("text", "").strip()
                    act_t = aw.get("text", "").strip()
                    # For equal paragraphs: only compare formatting for same text
                    # For replace paragraphs: compare any word at same position
                    if not exp_t or not act_t:
                        continue
                    txt = act_t  # label by actual text
                    is_m = self._is_list_marker(txt)

                    # ── Bold ──────────────────────────────────────────────────
                    eb, ab = _is_bold(ew), _is_bold(aw)
                    if eb != ab:
                        bold_changed_regions.append({
                            "text": txt,
                            "change": "bold_added" if ab else "bold_removed",
                            "bbox_exp": _scale_word(ew, exp_sx, exp_sy),
                            "bbox_act": _scale_word(aw, act_sx, act_sy),
                        })

                    # ── Size (independent of bold — a word can change BOTH) ──
                    es  = float(ew.get("size", 0) or 0)
                    as_ = float(aw.get("size", 0) or 0)
                    if es > 0 and as_ > 0 and abs(as_ - es) > _SIZE_TOLERANCE:
                        size_changed_regions.append({
                            "text": txt,
                            "exp_size": round(es, 1),
                            "act_size": round(as_, 1),
                            "change": "larger" if as_ > es else "smaller",
                            "bbox_exp": _scale_word(ew, exp_sx, exp_sy),
                            "bbox_act": _scale_word(aw, act_sx, act_sy),
                        })

                    # ── Alignment ─────────────────────────────────────────────
                    xs = float(aw.get("x0", 0)) - float(ew.get("x0", 0))
                    tol = _ALIGN_TOLERANCE_LIST if is_m else _ALIGN_TOLERANCE_BODY
                    if abs(xs) > tol:
                        rec = {
                            "text": txt, "x_shift_pts": round(xs, 1),
                            "direction": "right" if xs > 0 else "left",
                            "is_list_marker": is_m,
                            "bbox_exp": _scale_word(ew, exp_sx, exp_sy),
                            "bbox_act": _scale_word(aw, act_sx, act_sy),
                        }
                        (alignment_shifted_list if is_m else alignment_shifted_body).append(rec)

        for tag, i1, i2, j1, j2 in opcodes:
            if tag == "equal":
                for ep, ap in zip(exp_paras[i1:i2], act_paras[j1:j2]):
                    _check_paragraph_formatting(ep, ap)
            elif tag == "replace" and (i2 - i1) == (j2 - j1):
                # 1-to-1 paragraph replacement: check formatting changes
                # even though text content also changed
                for ep, ap in zip(exp_paras[i1:i2], act_paras[j1:j2]):
                    _check_paragraph_formatting(ep, ap)

        reportable_align_list = alignment_shifted_list
        reportable_align_body = alignment_shifted_body if len(alignment_shifted_body) >= _MIN_ALIGN_WORDS_BODY else []
        alignment_shifted_regions = reportable_align_list + reportable_align_body

        # ── Summaries ────────────────────────────────────────────────────────
        has_text_changes = bool(changes)
        has_fmt_changes  = bool(bold_changed_regions or size_changed_regions or alignment_shifted_regions)

        fmt_parts: List[str] = []
        if bold_changed_regions:
            b_add = [r for r in bold_changed_regions if r["change"] == "bold_added"]
            b_rem = [r for r in bold_changed_regions if r["change"] == "bold_removed"]
            if b_add: fmt_parts.append("Text became bold: " + ", ".join(repr(r["text"]) for r in b_add[:5]))
            if b_rem: fmt_parts.append("Text lost bold: "  + ", ".join(repr(r["text"]) for r in b_rem[:5]))
        if size_changed_regions:
            fmt_parts.append("Font size changed: " + ", ".join(
                f"{repr(r['text'])} ({r['exp_size']}pt→{r['act_size']}pt)" for r in size_changed_regions[:5]))
        if reportable_align_list:
            fmt_parts.append("List marker(s) alignment shifted: " +
                              ", ".join(repr(r["text"]) for r in reportable_align_list[:5]))
        if reportable_align_body:
            rn = sum(1 for r in reportable_align_body if r["direction"] == "right")
            ln = sum(1 for r in reportable_align_body if r["direction"] == "left")
            if rn: fmt_parts.append(f"{rn} body text element(s) indented right")
            if ln: fmt_parts.append(f"{ln} body text element(s) indented left")
        formatting_summary = " | ".join(fmt_parts)

        cl: List[str] = []
        for c in changes[:20]:
            ct = c["type"]
            if ct == "deleted":
                cl.append(f'  REMOVED: "{c["exp_text"][:120]}"')
            elif ct == "inserted":
                cl.append(f'  ADDED:   "{c["act_text"][:120]}"')
            elif ct == "modified":
                cl.append(f'  CHANGED: was="{c["exp_text"][:80]}"\n           now="{c["act_text"][:80]}"')
            elif ct == "replaced_block":
                cl.append(f'  BLOCK WAS: "{c["exp_text"][:100]}"\n  BLOCK NOW: "{c["act_text"][:100]}"')

        if cl:
            summary = "\n".join(cl)
            if formatting_summary:
                summary += "\n  FORMATTING: " + formatting_summary
        elif formatting_summary:
            summary = "FORMATTING: " + formatting_summary
        else:
            summary = "Text content identical — visual differences are rendering/watermark noise only."

        return {
            "changes":    changes,
            "added_regions":   added_regions,
            "removed_regions": removed_regions,
            "bold_changed_regions":      bold_changed_regions[:30],
            "size_changed_regions":      size_changed_regions[:30],
            "alignment_shifted_regions": alignment_shifted_regions[:30],
            "added_texts":   sorted(set(t.strip() for t in raw_added   if t.strip()))[:25],
            "removed_texts": sorted(set(t.strip() for t in raw_removed if t.strip()))[:25],
            "has_text_changes":       has_text_changes,
            "has_formatting_changes": has_fmt_changes,
            "formatting_summary": formatting_summary,
            "summary": summary,
        }

    # Per-pattern overlay colors: (R, G, B), alpha, diff-panel header label
    _PATTERN_COLORS: Dict[str, tuple] = {
        "page_wide":         ((220,  80,  80), 140, "VISUAL / GRAPHICAL CHANGE"),
        "header_only":       ((255,  50,  50), 160, "HEADER / FIELD CHANGE"),
        "header_and_fields": ((255,  50,  50), 160, "VALUE CHANGE"),
        "body_content":      ((255, 130,   0), 150, "CONTENT CHANGE"),
        "footer_area":       ((180,  60, 220), 160, "FOOTER / SIGNATURE"),
        "partial":           ((255, 200,   0), 140, "PARTIAL CHANGE"),
        "no_change":         ((  0, 200,   0),   0, "NO CHANGE"),
    }
    _DEFAULT_DIFF_COLOR: tuple = ((255, 50, 50), 150, "DIFFERENCE")

    def _resolve_diff_color(
        self,
        zone_analysis: Optional[Dict[str, Any]],
        signature_candidate: bool,
    ) -> tuple:
        """Return (rgb_tuple, alpha, label) for the diff overlay."""
        if signature_candidate:
            return (180, 60, 220), 155, "SIGNATURE / FOOTER"
        pattern = (zone_analysis or {}).get("change_pattern", "")
        return self._PATTERN_COLORS.get(pattern, self._DEFAULT_DIFF_COLOR)

    # Try to load a small font for labels; fall back to PIL default bitmap font
    @staticmethod
    def _get_small_font(size: int = 11):
        candidates = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
            "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
            "/System/Library/Fonts/Helvetica.ttc",
            "C:/Windows/Fonts/arial.ttf",
        ]
        for path in candidates:
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
        try:
            return ImageFont.load_default()
        except Exception:
            return None

    def _build_three_panel(
        self,
        expected: Image.Image,
        actual: Image.Image,
        mask: Image.Image,
        diff_regions: Optional[List[Tuple[int, int, int, int]]] = None,
        zone_analysis: Optional[Dict[str, Any]] = None,
        signature_candidate: bool = False,
        diff_counts: Optional[Dict[str, int]] = None,
    ) -> Image.Image:
        """
        Build three-panel comparison image with color legend strip.
        Diff panel overlay color:
          gray   = rendering / watermark noise
          red    = value / header field change
          orange = body content change
          purple = footer / signature area
          yellow = partial / mixed change

        Box colors on EXPECTED panel (left):
          red border = text removed from baseline

        Box colors on ACTUAL panel (middle):
          green  = text added in current version
          orange = bold formatting changed
          yellow = font size changed
          blue   = horizontal alignment / indentation shifted
        """
        rgb, alpha, diff_label = self._resolve_diff_color(zone_analysis, signature_candidate)
        r, g, b = rgb

        # Translucent colored overlay (original content stays readable)
        actual_rgba = actual.convert("RGBA")
        overlay = Image.new("RGBA", actual.size, (r, g, b, 0))
        mask_alpha = mask.point(lambda x: alpha if x > 0 else 0)
        overlay.putalpha(mask_alpha)
        diff_panel = Image.alpha_composite(actual_rgba, overlay).convert("RGB")

        # Draw bounding boxes in the same color around the most significant regions
        if diff_regions:
            draw = ImageDraw.Draw(diff_panel)
            for x0, y0, x1, y1 in diff_regions[:8]:
                draw.rectangle(
                    [
                        max(0, x0 - 2),
                        max(0, y0 - 2),
                        min(actual.width - 1, x1 + 2),
                        min(actual.height - 1, y1 + 2),
                    ],
                    outline=(r, g, b),
                    width=3,
                )

        w, h = actual.size

        # ── Build the three-panel composite ────────────────────────────────
        out = Image.new("RGB", (w * 3, h), (0, 0, 0))
        out.paste(expected, (0, 0))
        out.paste(actual, (w, 0))
        out.paste(diff_panel, (w * 2, 0))

        out = self._add_headers(out, w, diff_label=diff_label, diff_header_color=rgb,
                                diff_counts=diff_counts)

        # ── Append color-coded legend strip ────────────────────────────────
        legend = self._build_legend(w * 3)
        final = Image.new("RGB", (w * 3, out.height + legend.height), (10, 10, 10))
        final.paste(out, (0, 0))
        final.paste(legend, (0, out.height))
        return final

    def _build_legend(self, total_width: int) -> Image.Image:
        """Return a horizontal legend strip explaining the box colors."""
        legend_h = 36
        img = Image.new("RGB", (total_width, legend_h), (15, 15, 15))
        draw = ImageDraw.Draw(img)
        font = self._get_small_font(11)

        entries = [
            ((220, 50,  50),  "◼ Removed text (left panel)"),
            ((50,  200, 80),  "◼ Added text"),
            ((255, 140, 0),   "◼ Bold changed"),
            ((255, 210, 0),   "◼ Font size changed"),
            ((80,  160, 255), "◼ Alignment shifted"),
            ((180, 60,  220), "◼ Signature / footer"),
        ]

        x = 10
        y = 10
        gap = total_width // len(entries)
        for i, (color, label) in enumerate(entries):
            draw.text((x + i * gap, y), label, fill=color, font=font)

        return img

    def _add_headers(
        self,
        img: Image.Image,
        w: int,
        diff_label: str = "DIFF (HIGHLIGHTED)",
        diff_header_color: tuple = (240, 240, 240),
        diff_counts: Optional[Dict[str, int]] = None,
    ) -> Image.Image:
        font = self._get_small_font(12)
        draw = ImageDraw.Draw(img)
        header_h = 46
        draw.rectangle([0, 0, w * 3, header_h], fill=(20, 20, 20))

        # Build count summary for the diff panel header
        if diff_counts and any(diff_counts.values()):
            parts = []
            if diff_counts.get("added"):
                parts.append(f"+{diff_counts['added']} added")
            if diff_counts.get("removed"):
                parts.append(f"-{diff_counts['removed']} removed")
            if diff_counts.get("bold"):
                parts.append(f"{diff_counts['bold']} bold")
            if diff_counts.get("size"):
                parts.append(f"{diff_counts['size']} size")
            if diff_counts.get("align"):
                parts.append(f"{diff_counts['align']} align")
            count_str = "  |  " + "  ·  ".join(parts) if parts else ""
        else:
            count_str = ""

        labels = [
            ("EXPECTED (BASELINE)", (200, 200, 200), ""),
            ("ACTUAL (CURRENT)",    (200, 200, 200), ""),
            (f"DIFF — {diff_label}", diff_header_color, count_str),
        ]
        for idx, (text, color, suffix) in enumerate(labels):
            draw.text((idx * w + 12, 8),  text,   fill=color,        font=font)
            if suffix:
                draw.text((idx * w + 12, 26), suffix, fill=(180, 180, 180), font=font)
        return img

    def render_pages(
        self,
        pdf_path: str,
        result_id: Optional[str] = None,
        dpi: int = 150,
    ) -> List[Dict[str, Any]]:
        """Render each page of a single PDF as a single-panel PNG snapshot.

        Used in basic mode so the HTML report can show the form page in the
        Snapshot column even without a comparison partner.

        Returns a list of visual_validation-compatible dicts with:
        - page, snapshot_path, alignment_op="single",
          similarity=None, major=False, warn=False
        """
        rows: List[Dict[str, Any]] = []
        try:
            poppler = _poppler_path()
            pages = convert_from_path(pdf_path, dpi=dpi, fmt="png", poppler_path=poppler)
            base_name = Path(pdf_path).stem
            if result_id:
                base_name = f"{result_id}_{base_name}"

            for idx, page_img in enumerate(pages, start=1):
                img = page_img.convert("RGB")
                out_path = self.output_dir / f"{base_name}_basic_page{idx}.png"
                img.save(out_path, "PNG")
                rows.append({
                    "page": idx,
                    "expected_page_num": idx,
                    "actual_page_num": idx,
                    "alignment_op": "single",
                    "similarity": None,
                    "major": False,
                    "warn": False,
                    "note": "",
                    "snapshot_path": f"visual_diffs/{out_path.name}",
                    "diff_bbox": None,
                    "diff_pixels_pct": 0.0,
                    "diff_area_pct": 0.0,
                    "signature_candidate": False,
                    "signature_label": None,
                    "signature_reason": "",
                    "signature_confidence": "none",
                })
        except Exception as e:
            logger.warning("render_pages failed for %s: %s", pdf_path, e)
        return rows

    def render_pages_for_llm(
        self,
        pdf_path: str,
        dpi: int = 72,
        max_pages: int = 8,
        jpeg_quality: int = 70,
    ) -> List[Dict[str, Any]]:
        """Render PDF pages as base64 JPEG for multimodal LLM consumption.

        Keeps resolution low (72 DPI by default) to stay within token budgets:
        at 72 DPI a letter page is ~612×792 px ≈ 50 KB JPEG ≈ 70 KB base64.

        Returns:
            [{"page": 1, "b64": "<base64 string>", "mime": "image/jpeg"}, ...]
        """
        import base64
        import io

        results: List[Dict[str, Any]] = []
        try:
            poppler = _poppler_path()
            pages = convert_from_path(pdf_path, dpi=dpi, fmt="jpeg",
                                      poppler_path=poppler, last_page=max_pages)
            for idx, page_img in enumerate(pages, start=1):
                img = page_img.convert("RGB")
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=jpeg_quality, optimize=True)
                b64 = base64.standard_b64encode(buf.getvalue()).decode("ascii")
                results.append({"page": idx, "b64": b64, "mime": "image/jpeg"})
        except Exception as exc:
            logger.warning("render_pages_for_llm failed for %s: %s", pdf_path, exc)
        return results

    def annotate_snapshots_with_findings(
        self,
        pdf_path: str,
        result_json: Dict[str, Any],
        visual_entries: List[Dict[str, Any]],
    ) -> None:
        """Draw colored highlight boxes on page snapshots at the exact location
        of each LLM finding.  Modifies the PNG files in-place.

        Color coding matches legend:
          critical → red   high → orange   medium → yellow   low → blue
        """
        _SEVERITY_FILL = {
            "critical": (220, 30,  30,  110),
            "high":     (230, 110, 20,  100),
            "medium":   (230, 190, 0,   90),
            "low":      (70,  140, 230, 80),
        }
        _SEVERITY_OUTLINE = {
            "critical": (200, 0,   0),
            "high":     (200, 80,  0),
            "medium":   (180, 150, 0),
            "low":      (40,  100, 200),
        }

        # Build page → findings map from all finding buckets
        finding_buckets = [
            "spelling_errors", "format_issues", "value_mismatches",
            "missing_content", "extra_content", "layout_anomalies",
            "typography_issues", "structural_changes", "visual_mismatches",
            "compliance_issues", "accessibility_issues",
        ]
        findings_by_page: Dict[int, List[Dict[str, Any]]] = {}
        for bucket in finding_buckets:
            for item in (result_json.get(bucket) or []):
                if not isinstance(item, dict):
                    continue
                p = item.get("page")
                try:
                    p = int(str(p).strip()) if p not in (None, "") else None
                except Exception:
                    p = None
                if p is not None:
                    findings_by_page.setdefault(p, []).append(item)

        if not findings_by_page:
            return

        # Build page → snapshot path map from visual entries
        snap_map: Dict[int, str] = {}
        for ve in visual_entries:
            if not isinstance(ve, dict):
                continue
            p = ve.get("page")
            sp = ve.get("snapshot_path", "")
            if p and sp:
                # snapshot_path is relative like "visual_diffs/foo.png"
                abs_snap = self.output_dir / Path(sp).name
                snap_map[int(p)] = str(abs_snap)

        try:
            with pdfplumber.open(pdf_path) as pdf:
                for page_num, findings in findings_by_page.items():
                    snap_abs = snap_map.get(page_num)
                    if not snap_abs or not os.path.exists(snap_abs):
                        continue
                    if page_num < 1 or page_num > len(pdf.pages):
                        continue

                    try:
                        page = pdf.pages[page_num - 1]
                        page_w = float(page.width)
                        page_h = float(page.height)
                        words = page.extract_words(
                            keep_blank_chars=False, x_tolerance=3, y_tolerance=3
                        )

                        img = Image.open(snap_abs).convert("RGBA")
                        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
                        draw = ImageDraw.Draw(overlay)
                        font = self._get_small_font(9)

                        x_scale = img.width / page_w
                        y_scale = img.height / page_h

                        annotated = False
                        for idx, finding in enumerate(findings[:12], 1):
                            # Try to find the problem text on the page
                            search = (
                                finding.get("text")
                                or finding.get("snippet")
                                or finding.get("field_name")
                                or ""
                            ).strip()

                            bbox = self._find_text_bbox(words, search) if search and len(search) >= 3 else None

                            sev = str(finding.get("severity") or "low").lower()
                            fill = _SEVERITY_FILL.get(sev, _SEVERITY_FILL["low"])
                            outline = _SEVERITY_OUTLINE.get(sev, _SEVERITY_OUTLINE["low"])

                            if bbox:
                                x0, y0, x1, y1 = bbox
                                ix0 = max(0, int(x0 * x_scale) - 4)
                                iy0 = max(0, int(y0 * y_scale) - 4)
                                ix1 = min(img.width - 1, int(x1 * x_scale) + 4)
                                iy1 = min(img.height - 1, int(y1 * y_scale) + 4)
                                draw.rectangle([ix0, iy0, ix1, iy1], fill=fill, outline=outline + (220,), width=2)
                                # Finding number badge
                                bx, by = ix0, max(0, iy0 - 14)
                                draw.rectangle([bx, by, bx + 16, by + 13], fill=outline + (230,))
                                draw.text((bx + 3, by + 2), str(idx), fill=(255, 255, 255), font=font)
                                annotated = True

                        if annotated:
                            composite = Image.alpha_composite(img, overlay).convert("RGB")
                            composite.save(snap_abs, "PNG")
                    except Exception as _pe:
                        logger.debug("Annotation failed for page %d: %s", page_num, _pe)

        except Exception as e:
            logger.warning("annotate_snapshots_with_findings failed: %s", e)

    def _find_text_bbox(
        self,
        words: List[Dict[str, Any]],
        search_text: str,
    ) -> Optional[Tuple[float, float, float, float]]:
        """Locate search_text in a pdfplumber word list and return its bounding box."""
        if not words or not search_text:
            return None
        search_lower = search_text.lower().strip()
        tokens = search_lower.split()
        if not tokens:
            return None
        word_texts = [str(w.get("text", "")).lower() for w in words]

        # Exact token-sequence match (sliding window)
        n = len(tokens)
        for i in range(len(word_texts) - n + 1):
            if word_texts[i:i + n] == tokens:
                matched = words[i:i + n]
                return (
                    min(float(w["x0"]) for w in matched),
                    min(float(w["top"]) for w in matched),
                    max(float(w["x1"]) for w in matched),
                    max(float(w["bottom"]) for w in matched),
                )

        # Fallback: find any word that contains the longest token
        longest = max(tokens, key=len)
        if len(longest) >= 5:
            for i, wt in enumerate(word_texts):
                if longest in wt:
                    w = words[i]
                    return float(w["x0"]), float(w["top"]), float(w["x1"]), float(w["bottom"])

        return None

    # ---------------------------------------------------
    # Document-level comparison helpers
    # ---------------------------------------------------

    def compare_documents_metadata(self, expected_path: str, actual_path: str) -> Dict[str, Any]:
        """Compare PDF document-level metadata (title, author, subject, keywords, producer)."""
        result: Dict[str, Any] = {"changed": {}, "has_metadata_changes": False, "summary": ""}
        try:
            exp_meta: Dict[str, str] = {}
            act_meta: Dict[str, str] = {}
            with pdfplumber.open(expected_path) as pdf:
                exp_meta = {k: str(v or "") for k, v in (pdf.metadata or {}).items()}
            with pdfplumber.open(actual_path) as pdf:
                act_meta = {k: str(v or "") for k, v in (pdf.metadata or {}).items()}

            all_keys = set(exp_meta) | set(act_meta)
            for k in sorted(all_keys):
                ev = exp_meta.get(k, "")
                av = act_meta.get(k, "")
                if ev != av:
                    result["changed"][k] = {"expected": ev, "actual": av}

            result["has_metadata_changes"] = bool(result["changed"])
            if result["changed"]:
                parts = [f"{k}: '{v['expected']}' → '{v['actual']}'" for k, v in result["changed"].items()]
                result["summary"] = "Metadata changed: " + "; ".join(parts[:5])
        except Exception as e:
            logger.debug("Metadata comparison failed: %s", e)
        return result

    def compare_form_field_structure(self, expected_path: str, actual_path: str) -> Dict[str, Any]:
        """Compare AcroForm field names and types between two PDFs.

        Returns added_fields, removed_fields, changed_fields lists so the LLM
        can report structural field additions/removals without relying on visual
        pixel comparison alone.
        """
        result: Dict[str, Any] = {
            "added_fields": [],
            "removed_fields": [],
            "changed_fields": [],
            "has_structural_changes": False,
            "summary": "",
        }
        try:
            def _extract_fields(path: str) -> Dict[str, str]:
                fields: Dict[str, str] = {}
                with pdfplumber.open(path) as pdf:
                    for page in pdf.pages:
                        for annot in (page.annots or []):
                            data = annot.get("data") or {}
                            fname = data.get("T") or data.get("TU")
                            ftype = data.get("FT")
                            if fname:
                                fields[str(fname)] = str(ftype or "unknown")
                return fields

            exp_f = _extract_fields(expected_path)
            act_f = _extract_fields(actual_path)
            exp_names = set(exp_f)
            act_names = set(act_f)

            result["removed_fields"] = sorted(exp_names - act_names)
            result["added_fields"] = sorted(act_names - exp_names)
            for name in sorted(exp_names & act_names):
                if exp_f[name] != act_f[name]:
                    result["changed_fields"].append({
                        "name": name,
                        "expected_type": exp_f[name],
                        "actual_type": act_f[name],
                    })

            result["has_structural_changes"] = bool(
                result["added_fields"] or result["removed_fields"] or result["changed_fields"]
            )
            if result["has_structural_changes"]:
                parts = []
                if result["removed_fields"]:
                    parts.append(f"Removed fields: {', '.join(result['removed_fields'][:5])}")
                if result["added_fields"]:
                    parts.append(f"Added fields: {', '.join(result['added_fields'][:5])}")
                if result["changed_fields"]:
                    parts.append(f"Type changes: {len(result['changed_fields'])} field(s)")
                result["summary"] = "; ".join(parts)
        except Exception as e:
            logger.debug("Form field structure comparison failed: %s", e)
        return result

        return img