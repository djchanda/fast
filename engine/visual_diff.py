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
        Returns per-page visual comparison rows.

        Output row shape:
        {
            "page": 3,
            "similarity": 0.998,
            "major": False,
            "warn": True,
            "note": "...",
            "snapshot_path": "...",
            "diff_bbox": [x0, y0, x1, y1] | None,
            "diff_pixels_pct": 0.1234,
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

            max_pages = max(len(expected_pages), len(actual_pages))
            if max_pages == 0:
                return []

            base_name = Path(original_pdf_path).stem
            if result_id:
                base_name = f"{result_id}_{base_name}"

            rows: List[Dict[str, Any]] = []

            fallback_size = None
            if expected_pages:
                fallback_size = expected_pages[0].size
            elif actual_pages:
                fallback_size = actual_pages[0].size

            for i in range(max_pages):
                page_num = i + 1

                exp = expected_pages[i] if i < len(expected_pages) else self._create_blank_image(fallback_size)
                act = actual_pages[i] if i < len(actual_pages) else self._create_blank_image(fallback_size)

                exp = exp.convert("RGB")
                act = act.convert("RGB")
                exp, act = self._normalize_sizes(exp, act)

                similarity, diff_pct, mask = self._compute_similarity_and_mask(act, exp)

                diff_bbox = self._get_diff_bbox(mask)
                diff_area_pct = self._bbox_area_pct(diff_bbox, exp.size) if diff_bbox else 0.0

                # Tuned thresholds:
                # - major: only strong whole-page / content-level differences
                # - warn: smaller but real visible differences
                major = diff_pct >= 2.0
                warn = (diff_pct >= 0.10) and not major

                note = "No material visual differences detected."
                if major:
                    note = "Significant content-level visual differences detected."
                elif warn:
                    note = "Minor visual differences detected."

                signature_candidate = False
                signature_label = None
                signature_reason = ""
                signature_confidence = "none"

                if diff_bbox:
                    signature_labels = self._find_signature_labels(expected_pdf_path, page_num, exp.size)
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

                    # Only elevate note when signature detection is actually credible
                    if signature_candidate:
                        warn = True
                        if signature_confidence == "high":
                            note = "Possible missing or changed signature detected."
                        else:
                            note = "Possible signature-region change detected."

                panel = self._build_three_panel(exp, act, mask)
                out_path = self.output_dir / f"{base_name}_page{page_num}.png"
                panel.save(out_path, "PNG")

                rows.append(
                    {
                        "page": page_num,
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
                    }
                )

            return rows

        except Exception as e:
            logger.error(f"Error comparing PDFs (detailed): {str(e)}", exc_info=True)
            return []

    def compare_pdfs(self, original_pdf_path: str, expected_pdf_path: str, result_id: Optional[str] = None) -> List[str]:
        rows = self.compare_pdfs_detailed(original_pdf_path, expected_pdf_path, result_id=result_id)
        return [r.get("snapshot_path") for r in rows if r.get("snapshot_path")]

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

        # Keep sensitivity high enough for missing signatures, but not too low
        threshold = 18
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

    def _build_three_panel(self, expected: Image.Image, actual: Image.Image, mask: Image.Image) -> Image.Image:
        red = Image.new("RGB", actual.size, (255, 0, 0))
        overlay = Image.composite(red, actual, mask)
        overlay = ImageEnhance.Brightness(overlay).enhance(1.05)

        w, h = actual.size
        out = Image.new("RGB", (w * 3, h), (0, 0, 0))

        out.paste(expected, (0, 0))
        out.paste(actual, (w, 0))
        out.paste(overlay, (w * 2, 0))

        out = self._add_headers(out, w)
        return out

    def _add_headers(self, img: Image.Image, w: int) -> Image.Image:
        draw = ImageDraw.Draw(img)
        header_h = 40
        draw.rectangle([0, 0, w * 3, header_h], fill=(20, 20, 20))

        labels = ["EXPECTED", "ACTUAL", "DIFF (HIGHLIGHTED)"]
        for idx, text in enumerate(labels):
            x = idx * w + 12
            y = 10
            draw.text((x, y), text, fill=(240, 240, 240))

        return img