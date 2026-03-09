from __future__ import annotations

import os
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from pdf2image import convert_from_path
from PIL import Image, ImageChops, ImageEnhance, ImageDraw

logger = logging.getLogger(__name__)

try:
    import numpy as np
except Exception:
    np = None


def _get_poppler_path() -> Optional[str]:
    """
    Prefer POPPLER_PATH env var. If absent, try common Windows path.
    If neither exists, return None so pdf2image can try PATH.
    """
    candidates = [
        os.getenv("POPPLER_PATH", "").strip(),
        r"C:\Poppler\Release-25.12.0-0\poppler-25.12.0\Library\bin",
    ]

    for p in candidates:
        if p and os.path.isdir(p):
            return p
    return None


class VisualDiff:
    """
    Generates visual diffs between two PDFs and returns structured results.
    """

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
        crop_margins_pct: float = 0.02,
        diff_threshold: Optional[int] = None,
        min_region_area_pct: float = 0.02,
        major_diff_pct: float = 2.0,
        warn_diff_pct: float = 0.3,
    ) -> List[Dict[str, Any]]:
        try:
            poppler = _get_poppler_path()

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

            fallback_size = None
            if expected_pages:
                fallback_size = expected_pages[0].size
            elif actual_pages:
                fallback_size = actual_pages[0].size

            rows: List[Dict[str, Any]] = []

            for i in range(max_pages):
                page_num = i + 1

                exp = expected_pages[i] if i < len(expected_pages) else self._blank(fallback_size)
                act = actual_pages[i] if i < len(actual_pages) else self._blank(fallback_size)

                exp = exp.convert("RGB")
                act = act.convert("RGB")
                exp, act = self._normalize_sizes(exp, act)

                exp_cmp = self._crop_margins(exp, crop_margins_pct)
                act_cmp = self._crop_margins(act, crop_margins_pct)

                exp_cmp = self._normalize_image(exp_cmp)
                act_cmp = self._normalize_image(act_cmp)

                similarity, diff_pct, mask = self._diff_mask(act_cmp, exp_cmp, diff_threshold)
                regions = self._extract_regions(mask, min_region_area_pct=min_region_area_pct)
                category_hint = self._infer_category_hint(exp_cmp.size, regions)

                major = diff_pct >= major_diff_pct
                warn = (diff_pct >= warn_diff_pct) and not major

                note = "No material visual differences detected."
                if major:
                    note = "Significant content-level visual differences detected."
                elif warn:
                    note = "Minor visual differences detected."

                panel = self._build_three_panel(exp, act, exp_cmp.size, mask, crop_margins_pct)

                out_path = self.output_dir / f"{base_name}_page{page_num}.png"
                panel.save(out_path, "PNG")

                rows.append(
                    {
                        "page": page_num,
                        "similarity": round(similarity, 3),
                        "diff_pixels_pct": round(diff_pct, 3),
                        "major": bool(major),
                        "warn": bool(warn),
                        "note": note,
                        "category_hint": category_hint,
                        "region_count": len(regions),
                        "diff_regions": regions,
                        "snapshot_path": f"visual_diffs/{out_path.name}",
                    }
                )

            return rows

        except Exception as e:
            logger.error(f"Error comparing PDFs (detailed): {str(e)}", exc_info=True)
            return []

    def compare_pdfs(self, original_pdf_path: str, expected_pdf_path: str, result_id: Optional[str] = None) -> List[str]:
        rows = self.compare_pdfs_detailed(original_pdf_path, expected_pdf_path, result_id=result_id)
        return [r.get("snapshot_path") for r in rows if r.get("snapshot_path")]

    def _blank(self, size: Optional[Tuple[int, int]]) -> Image.Image:
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
            a = self._pad(a, (max_w, max_h))
        if b.size != (max_w, max_h):
            b = self._pad(b, (max_w, max_h))
        return a, b

    def _pad(self, img: Image.Image, target: Tuple[int, int]) -> Image.Image:
        bg = Image.new("RGB", target, (255, 255, 255))
        bg.paste(img, (0, 0))
        return bg

    def _crop_margins(self, img: Image.Image, pct: float) -> Image.Image:
        if pct <= 0:
            return img
        w, h = img.size
        dx = int(w * pct)
        dy = int(h * pct)
        return img.crop((dx, dy, w - dx, h - dy))

    def _normalize_image(self, img: Image.Image) -> Image.Image:
        img = ImageEnhance.Contrast(img).enhance(1.05)
        img = ImageEnhance.Brightness(img).enhance(1.02)
        return img

    def _otsu_threshold(self, gray: Image.Image) -> int:
        if np is None:
            return 30

        arr = np.array(gray)
        hist, _ = np.histogram(arr.flatten(), bins=256, range=(0, 256))
        total = arr.size
        sum_total = np.dot(np.arange(256), hist)

        sum_b = 0.0
        w_b = 0.0
        var_max = 0.0
        threshold = 30

        for t in range(256):
            w_b += hist[t]
            if w_b == 0:
                continue
            w_f = total - w_b
            if w_f == 0:
                break
            sum_b += t * hist[t]
            m_b = sum_b / w_b
            m_f = (sum_total - sum_b) / w_f
            var_between = w_b * w_f * (m_b - m_f) ** 2
            if var_between > var_max:
                var_max = var_between
                threshold = t

        return max(15, min(60, int(threshold)))

    def _diff_mask(
        self,
        actual: Image.Image,
        expected: Image.Image,
        threshold: Optional[int],
    ) -> Tuple[float, float, Image.Image]:
        diff = ImageChops.difference(actual, expected).convert("L")
        thr = threshold if threshold is not None else self._otsu_threshold(diff)

        mask = diff.point(lambda x: 255 if x > thr else 0)

        if np is not None:
            m = (np.array(mask) > 0).astype(np.uint8)
            padded = np.pad(m, ((1, 1), (1, 1)), mode="constant")
            neighbors = (
                padded[0:-2, 0:-2] + padded[0:-2, 1:-1] + padded[0:-2, 2:] +
                padded[1:-1, 0:-2] + padded[1:-1, 1:-1] + padded[1:-1, 2:] +
                padded[2:, 0:-2] + padded[2:, 1:-1] + padded[2:, 2:]
            )
            m2 = (neighbors >= 3).astype(np.uint8)
            mask = Image.fromarray((m2 * 255).astype(np.uint8), mode="L")

        diff_pixels = sum(1 for px in mask.getdata() if px > 0)
        total_pixels = actual.width * actual.height if actual.width and actual.height else 0
        diff_pct = (diff_pixels / total_pixels) * 100.0 if total_pixels else 0.0
        similarity = max(0.0, min(1.0, (100.0 - diff_pct) / 100.0))
        return similarity, diff_pct, mask

    def _extract_regions(self, mask: Image.Image, min_region_area_pct: float) -> List[Dict[str, Any]]:
        if np is None:
            return []

        m = (np.array(mask) > 0).astype(np.uint8)
        h, w = m.shape
        total = w * h
        min_area = max(1, int(total * (min_region_area_pct / 100.0)))

        visited = np.zeros_like(m, dtype=np.uint8)
        regions: List[Dict[str, Any]] = []

        def bfs(sy: int, sx: int):
            q = [(sy, sx)]
            visited[sy, sx] = 1
            ys = [sy]
            xs = [sx]
            count = 0
            while q:
                y, x = q.pop()
                count += 1
                for ny, nx in ((y - 1, x), (y + 1, x), (y, x - 1), (y, x + 1)):
                    if 0 <= ny < h and 0 <= nx < w and visited[ny, nx] == 0 and m[ny, nx] == 1:
                        visited[ny, nx] = 1
                        q.append((ny, nx))
                        ys.append(ny)
                        xs.append(nx)
            return count, min(xs), min(ys), max(xs), max(ys)

        for y in range(h):
            for x in range(w):
                if m[y, x] == 1 and visited[y, x] == 0:
                    area, x1, y1, x2, y2 = bfs(y, x)
                    if area >= min_area:
                        regions.append(
                            {
                                "bbox": [int(x1), int(y1), int(x2), int(y2)],
                                "area_px": int(area),
                                "area_pct": round((area / total) * 100.0, 4),
                            }
                        )

        regions.sort(key=lambda r: r["area_px"], reverse=True)
        return regions[:50]

    def _infer_category_hint(self, img_size: Tuple[int, int], regions: List[Dict[str, Any]]) -> str:
        if not regions:
            return "no_visual_change"

        w, h = img_size
        biggest = regions[0]
        x1, y1, x2, y2 = biggest["bbox"]
        mid_y = (y1 + y2) / 2.0
        mid_x = (x1 + x2) / 2.0

        if mid_y > h * 0.70:
            return "lower_page_visual_change"
        if mid_y < h * 0.18:
            return "header_or_logo_change"
        if mid_x > w * 0.65 and mid_y > h * 0.50:
            return "right_lower_visual_change"
        return "unclassified_visual_change"

    def _build_three_panel(
        self,
        expected_full: Image.Image,
        actual_full: Image.Image,
        cropped_size: Tuple[int, int],
        mask_cropped: Image.Image,
        crop_margins_pct: float,
    ) -> Image.Image:
        exp = expected_full.copy()
        act = actual_full.copy()

        w_full, h_full = act.size
        dx = int(w_full * crop_margins_pct) if crop_margins_pct > 0 else 0
        dy = int(h_full * crop_margins_pct) if crop_margins_pct > 0 else 0

        full_mask = Image.new("L", (w_full, h_full), 0)
        full_mask.paste(mask_cropped, (dx, dy))

        red = Image.new("RGB", act.size, (255, 0, 0))
        overlay = Image.composite(red, act, full_mask)
        overlay = ImageEnhance.Brightness(overlay).enhance(1.05)

        out = Image.new("RGB", (w_full * 3, h_full), (0, 0, 0))
        out.paste(exp, (0, 0))
        out.paste(act, (w_full, 0))
        out.paste(overlay, (w_full * 2, 0))

        return self._add_headers(out, w_full)

    def _add_headers(self, img: Image.Image, w: int) -> Image.Image:
        draw = ImageDraw.Draw(img)
        header_h = 40
        draw.rectangle([0, 0, w * 3, header_h], fill=(20, 20, 20))
        labels = ["EXPECTED", "ACTUAL", "DIFF (HIGHLIGHTED)"]
        for idx, text in enumerate(labels):
            draw.text((idx * w + 12, 10), text, fill=(240, 240, 240))
        return img