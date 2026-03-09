# engine/visual_diff.py
from __future__ import annotations

import os
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from pdf2image import convert_from_path
from PIL import Image, ImageChops, ImageEnhance, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

# Your Poppler bin folder (must contain pdfinfo.exe and pdftoppm.exe)
POPPLER_BIN = r"C:\Poppler\Release-25.12.0-0\poppler-25.12.0\Library\bin"


def _poppler_path() -> str:
    """
    Return Poppler bin path. We hardcode to avoid PATH/env issues.
    Raises a clear error if the folder doesn't exist.
    """
    if not os.path.isdir(POPPLER_BIN):
        raise RuntimeError(f"Poppler bin folder not found: {POPPLER_BIN}")
    return POPPLER_BIN


class VisualDiff:
    """
    Generates visual diffs between two PDFs.
    Output images are stored in instance/visual_diffs (or configured output_dir).
    """

    def __init__(self, output_dir: Optional[str | Path] = None):
        if output_dir is None:
            # Default: create visual_diffs folder under current working directory
            output_dir = Path.cwd() / "visual_diffs"
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    # -----------------------
    # Public API (CLI/UI)
    # -----------------------
    def compare_pdfs_detailed(
        self,
        original_pdf_path: str,
        expected_pdf_path: str,
        result_id: Optional[str] = None,
        dpi: int = 150,
    ) -> List[Dict[str, Any]]:
        """
        CLI/UI expected output:
        [
          {
            "page": 1,
            "similarity": 0.945,
            "major": True,
            "warn": False,
            "note": "Major visual differences detected.",
            "snapshot_path": "visual_diffs/<file>.png"
          },
          ...
        ]

        Generates batchPDF-style 3-panel PNG for each page:
        Expected | Actual | Diff Overlay (red highlights).
        """

        try:
            poppler = _poppler_path()

            # Render PDFs into page images
            expected_pages = convert_from_path(
                expected_pdf_path, dpi=dpi, fmt="png", poppler_path=poppler
            )
            actual_pages = convert_from_path(
                original_pdf_path, dpi=dpi, fmt="png", poppler_path=poppler
            )

            max_pages = max(len(expected_pages), len(actual_pages))
            if max_pages == 0:
                return []

            base_name = Path(original_pdf_path).stem
            if result_id:
                base_name = f"{result_id}_{base_name}"

            rows: List[Dict[str, Any]] = []

            # If one PDF has 0 pages (weird but possible), pick a size from the other
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

                # Normalize to same size
                exp, act = self._normalize_sizes(exp, act)

                # Compute diff + similarity
                similarity, diff_pct, mask = self._compute_similarity_and_mask(act, exp)

                # Determine severity thresholds (tune if needed)
                # diff_pct is % of pixels considered "different" after thresholding
                major = diff_pct >= 2.0
                warn = (diff_pct >= 0.3) and not major

                note = "No material visual differences detected."
                if major:
                    note = "Significant content-level visual differences detected."
                elif warn:
                    note = "Minor visual differences detected."

                # Build 3-panel image: Expected | Actual | Diff Overlay
                panel = self._build_three_panel(exp, act, mask)

                # Save image
                out_path = self.output_dir / f"{base_name}_page{page_num}.png"
                panel.save(out_path, "PNG")

                rows.append(
                    {
                        "page": page_num,
                        # Report can show either 0-1 or %; we give 0-1
                        "similarity": round(similarity, 3),
                        "major": bool(major),
                        "warn": bool(warn),
                        "note": note,
                        # IMPORTANT: store relative path (UI will convert to route link)
                        "snapshot_path": f"visual_diffs/{out_path.name}",
                    }
                )

            return rows

        except Exception as e:
            logger.error(f"Error comparing PDFs (detailed): {str(e)}", exc_info=True)
            return []

    # (Optional) keep a simpler API if other code calls it
    def compare_pdfs(self, original_pdf_path: str, expected_pdf_path: str, result_id: Optional[str] = None) -> List[str]:
        """
        Backward-compatible: returns list of PNG paths.
        """
        rows = self.compare_pdfs_detailed(original_pdf_path, expected_pdf_path, result_id=result_id)
        return [r.get("snapshot_path") for r in rows if r.get("snapshot_path")]

    # -----------------------
    # Internals
    # -----------------------
    def _create_blank_image(self, size: Optional[Tuple[int, int]]) -> Image.Image:
        if not size:
            size = (1000, 1400)
        img = Image.new("RGB", size, (255, 255, 255))
        draw = ImageDraw.Draw(img)
        draw.text((20, 20), "Blank Page", fill=(80, 80, 80))
        return img

    def _normalize_sizes(self, a: Image.Image, b: Image.Image) -> Tuple[Image.Image, Image.Image]:
        """
        Ensure both images have the same size by padding to max dimensions.
        """
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
        """
        Compute:
          - similarity in range [0..1]
          - diff_pct in [0..100] percentage of pixels different
          - a binary mask (255 where different)
        """
        # Pixel difference -> grayscale
        diff = ImageChops.difference(actual, expected).convert("L")

        # Threshold controls noise sensitivity. Higher = less sensitive.
        threshold = 30

        # Binary mask: 255 where diff > threshold
        mask = diff.point(lambda x: 255 if x > threshold else 0)

        diff_pixels = sum(1 for px in mask.getdata() if px > 0)
        total_pixels = actual.width * actual.height if actual.width and actual.height else 0

        diff_pct = (diff_pixels / total_pixels) * 100.0 if total_pixels else 0.0
        similarity = max(0.0, min(1.0, (100.0 - diff_pct) / 100.0))

        return similarity, diff_pct, mask

    def _build_three_panel(self, expected: Image.Image, actual: Image.Image, mask: Image.Image) -> Image.Image:
        """
        Build Expected | Actual | Diff Overlay (red).
        """
        # Overlay red on "actual" where differences exist
        red = Image.new("RGB", actual.size, (255, 0, 0))
        overlay = Image.composite(red, actual, mask)
        overlay = ImageEnhance.Brightness(overlay).enhance(1.05)

        # Create canvas
        w, h = actual.size
        out = Image.new("RGB", (w * 3, h), (0, 0, 0))

        out.paste(expected, (0, 0))
        out.paste(actual, (w, 0))
        out.paste(overlay, (w * 2, 0))

        # Optional headers (makes it look like batchPDF)
        out = self._add_headers(out, w, h)

        return out

    def _add_headers(self, img: Image.Image, w: int, h: int) -> Image.Image:
        """
        Adds labels: EXPECTED | ACTUAL | DIFF
        """
        draw = ImageDraw.Draw(img)
        header_h = 40
        # dark header strip
        draw.rectangle([0, 0, w * 3, header_h], fill=(20, 20, 20))

        labels = ["EXPECTED", "ACTUAL", "DIFF (HIGHLIGHTED)"]
        for idx, text in enumerate(labels):
            x = idx * w + 12
            y = 10
            draw.text((x, y), text, fill=(240, 240, 240))

        return img
