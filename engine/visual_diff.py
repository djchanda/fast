# engine/visual_diff.py
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pdfplumber
from pdf2image import convert_from_path
from PIL import Image, ImageChops, ImageDraw, ImageEnhance

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
                    blank = self._create_blank_image(act.size)
                    blank_n, act_n = self._normalize_sizes(blank, act)
                    empty_mask = Image.new("L", act_n.size, 0)
                    panel = self._build_three_panel(blank_n, act_n, empty_mask)
                    out_path = self.output_dir / f"{base_name}_page{output_row_num}.png"
                    panel.save(out_path, "PNG")
                    rows.append({
                        "page": output_row_num,
                        "expected_page_num": None,
                        "actual_page_num": act_idx + 1,
                        "alignment_op": "inserted",
                        "similarity": 0.0,
                        "major": True,
                        "warn": False,
                        "note": (
                            f"Page {act_idx + 1} of the actual PDF has no counterpart "
                            f"in the expected PDF — this is an extra / inserted page."
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

                # If text is identical, suppress major/warn flags for the diff overlay
                # (keeps pixel-diff data but marks it as noise)
                if not text_diff["has_text_changes"] and zone_analysis.get("change_pattern") == "page_wide":
                    major = False
                    warn = False if diff_pct < 20.0 else warn

                note = self._generate_semantic_note(diff_pct, zone_analysis, major, warn)
                if text_diff["summary"]:
                    note = f"{note} TEXT: {text_diff['summary']}"

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

                # Annotate expected panel: red boxes on words present in baseline but gone
                exp_annotated = exp.copy()
                if text_diff["removed_regions"]:
                    draw_e = ImageDraw.Draw(exp_annotated)
                    for region in text_diff["removed_regions"][:40]:
                        x0, y0, x1, y1 = region["bbox"]
                        draw_e.rectangle(
                            [max(0, x0 - 1), max(0, y0 - 1),
                             min(exp.width - 1, x1 + 1), min(exp.height - 1, y1 + 1)],
                            outline=(220, 50, 50), width=2,
                        )

                # Annotate actual panel: green boxes on words added in current
                act_annotated = act.copy()
                if text_diff["added_regions"]:
                    draw_a = ImageDraw.Draw(act_annotated)
                    for region in text_diff["added_regions"][:40]:
                        x0, y0, x1, y1 = region["bbox"]
                        draw_a.rectangle(
                            [max(0, x0 - 1), max(0, y0 - 1),
                             min(act.width - 1, x1 + 1), min(act.height - 1, y1 + 1)],
                            outline=(50, 200, 80), width=2,
                        )

                panel = self._build_three_panel(
                    exp_annotated, act_annotated, mask,
                    diff_regions, zone_analysis, signature_candidate,
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

        diff_pixels = sum(1 for px in mask.getdata() if px > 0)
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

    def _build_text_diff_annotations(
        self,
        expected_pdf_path: str,
        actual_pdf_path: str,
        expected_page_num: int,
        actual_page_num: int,
        image_size: Tuple[int, int],
    ) -> Dict[str, Any]:
        """
        Word-level text comparison between two PDF pages using pdfplumber.

        Returns word bounding boxes for added/removed text and a plain-English
        summary.  The caller annotates the rendered page images with these boxes
        so the three-panel image explicitly shows WHAT text changed (green =
        added in current, red = present in baseline but gone in current).
        """
        def _extract(pdf_path: str, page_num: int):
            try:
                with pdfplumber.open(pdf_path) as pdf:
                    if not (1 <= page_num <= len(pdf.pages)):
                        return [], 612.0, 792.0
                    page = pdf.pages[page_num - 1]
                    words = page.extract_words() or []
                    return words, float(page.width or 612.0), float(page.height or 792.0)
            except Exception:
                return [], 612.0, 792.0

        exp_words, exp_pw, exp_ph = _extract(expected_pdf_path, expected_page_num)
        act_words, act_pw, act_ph = _extract(actual_pdf_path, actual_page_num)

        img_w, img_h = image_size
        exp_sx, exp_sy = img_w / exp_pw, img_h / exp_ph
        act_sx, act_sy = img_w / act_pw, img_h / act_ph

        def _scale(word: dict, sx: float, sy: float) -> Tuple[int, int, int, int]:
            return (
                int(float(word.get("x0", 0)) * sx),
                int(float(word.get("top", 0)) * sy),
                int(float(word.get("x1", 0)) * sx),
                int(float(word.get("bottom", 0)) * sy),
            )

        def _clean_set(words):
            return {
                w["text"].strip()
                for w in words
                if len(w.get("text", "").strip()) > 1
                and w["text"].strip().upper() not in self._WATERMARK_WORDS
            }

        exp_set = _clean_set(exp_words)
        act_set = _clean_set(act_words)
        added_texts = act_set - exp_set
        removed_texts = exp_set - act_set

        # Locate added words in actual image coordinates
        added_regions: List[Dict] = []
        for w in act_words:
            txt = w.get("text", "").strip()
            if txt in added_texts:
                added_regions.append({"text": txt, "bbox": _scale(w, act_sx, act_sy)})

        # Locate removed words in expected image coordinates
        removed_regions: List[Dict] = []
        for w in exp_words:
            txt = w.get("text", "").strip()
            if txt in removed_texts:
                removed_regions.append({"text": txt, "bbox": _scale(w, exp_sx, exp_sy)})

        has_changes = bool(added_texts or removed_texts)
        if not has_changes:
            summary = "Text content identical — visual differences are rendering/watermark noise only."
        else:
            parts = []
            if added_texts:
                parts.append("Added: " + ", ".join(repr(t) for t in sorted(added_texts)[:10]))
            if removed_texts:
                parts.append("Removed: " + ", ".join(repr(t) for t in sorted(removed_texts)[:10]))
            summary = " | ".join(parts)

        return {
            "added_regions": added_regions,
            "removed_regions": removed_regions,
            "added_texts": sorted(added_texts),
            "removed_texts": sorted(removed_texts),
            "summary": summary,
            "has_text_changes": has_changes,
        }

    # Per-pattern overlay colors: (R, G, B), alpha, diff-panel header label
    _PATTERN_COLORS: Dict[str, tuple] = {
        "page_wide":         ((140, 140, 140), 80,  "RENDERING / WATERMARK NOISE"),
        "header_only":       ((255,  50,  50), 150, "HEADER / FIELD CHANGE"),
        "header_and_fields": ((255,  50,  50), 150, "VALUE CHANGE"),
        "body_content":      ((255, 130,   0), 140, "CONTENT CHANGE"),
        "footer_area":       ((180,  60, 220), 150, "FOOTER / SIGNATURE"),
        "partial":           ((255, 200,   0), 130, "PARTIAL CHANGE"),
        "no_change":         ((  0, 200,   0),   0, "NO CHANGE"),
    }
    _DEFAULT_DIFF_COLOR: tuple = ((255, 50, 50), 140, "DIFFERENCE")

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

    def _build_three_panel(
        self,
        expected: Image.Image,
        actual: Image.Image,
        mask: Image.Image,
        diff_regions: Optional[List[Tuple[int, int, int, int]]] = None,
        zone_analysis: Optional[Dict[str, Any]] = None,
        signature_candidate: bool = False,
    ) -> Image.Image:
        """
        Build three-panel comparison image.
        Diff panel uses a pattern-specific color:
          gray   = rendering / watermark noise
          red    = value / header field change
          orange = body content change
          purple = footer / signature area
          yellow = partial / mixed change
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
        out = Image.new("RGB", (w * 3, h), (0, 0, 0))
        out.paste(expected, (0, 0))
        out.paste(actual, (w, 0))
        out.paste(diff_panel, (w * 2, 0))

        out = self._add_headers(out, w, diff_label=diff_label, diff_header_color=rgb)
        return out

    def _add_headers(
        self,
        img: Image.Image,
        w: int,
        diff_label: str = "DIFF (HIGHLIGHTED)",
        diff_header_color: tuple = (240, 240, 240),
    ) -> Image.Image:
        draw = ImageDraw.Draw(img)
        header_h = 40
        draw.rectangle([0, 0, w * 3, header_h], fill=(20, 20, 20))

        labels = [
            ("EXPECTED (BASELINE)", (200, 200, 200)),
            ("ACTUAL (CURRENT)",    (200, 200, 200)),
            (f"DIFF — {diff_label}", diff_header_color),
        ]
        for idx, (text, color) in enumerate(labels):
            draw.text((idx * w + 12, 10), text, fill=color)

        return img