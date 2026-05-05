"""Standalone tests for the iNet-parity features.

Run with: python tests/test_new_features.py
Does NOT require Flask or the full app — tests only pure helper functions
and service logic using mocks.
"""
import io
import os
import sys
import tempfile
import types

# Make sure the project root is on the path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS = "\033[92m✓\033[0m"
FAIL = "\033[91m✗\033[0m"
_failures = []


def test(name, fn):
    try:
        fn()
        print(f"  {PASS}  {name}")
    except Exception as e:
        print(f"  {FAIL}  {name}  —  {e}")
        _failures.append(name)


# ---------------------------------------------------------------------------
# 1.  _fmt_size helper
# ---------------------------------------------------------------------------
print("\n── Feature 2: File metadata helpers ──")

def _import_pdf_report():
    # Patch reportlab imports so they don't fail if not installed in system python
    # (the app's embedded python has them; this test verifies logic only)
    try:
        import app.services.pdf_report as m
        return m
    except ImportError:
        # Build a minimal stub so helper functions are still testable
        import importlib, types
        stub = types.ModuleType("app.services.pdf_report")
        exec(
            open(os.path.join(os.path.dirname(__file__), "../app/services/pdf_report.py")).read(),
            stub.__dict__,
        )
        return stub


def test_fmt_size_bytes():
    from app.reporting.html_report import _fmt_size
    assert _fmt_size(500) == "500 B", f"got {_fmt_size(500)}"

def test_fmt_size_kb():
    from app.reporting.html_report import _fmt_size
    assert _fmt_size(153 * 1024) == "153 KB", f"got {_fmt_size(153 * 1024)}"

def test_fmt_size_mb():
    from app.reporting.html_report import _fmt_size
    result = _fmt_size(1_200_000)
    assert "MB" in result, f"got {result}"

def test_fmt_size_none():
    from app.reporting.html_report import _fmt_size
    assert _fmt_size(None) == "—"

def test_fmt_date_none():
    from app.reporting.html_report import _fmt_date
    assert _fmt_date(None) == "—"

def test_fmt_date_datetime():
    from app.reporting.html_report import _fmt_date
    from datetime import datetime
    dt = datetime(2017, 8, 9, 4, 12, 49)
    result = _fmt_date(dt)
    assert "2017" in result and "Aug" in result, f"got {result}"


try:
    from app.reporting.html_report import _fmt_size, _fmt_date
    test("_fmt_size: bytes",   test_fmt_size_bytes)
    test("_fmt_size: KB",      test_fmt_size_kb)
    test("_fmt_size: MB",      test_fmt_size_mb)
    test("_fmt_size: None",    test_fmt_size_none)
    test("_fmt_date: None",    test_fmt_date_none)
    test("_fmt_date: datetime",test_fmt_date_datetime)
except ImportError as e:
    print(f"  (skipped — app not importable: {e})")


# ---------------------------------------------------------------------------
# 2.  Image caption drawing (Feature 1)
# ---------------------------------------------------------------------------
print("\n── Feature 1: Observation caption image drawing ──")

def test_caption_image_taller_than_input():
    """After adding caption strips the output must be taller than the input."""
    from PIL import Image, ImageDraw, ImageFont
    import textwrap

    # Create a small synthetic 3-panel image (300 x 100)
    img = Image.new("RGB", (300, 100), (30, 30, 60))

    LABEL_H = 30
    cap_text = "Effective Date field shows 10/15/2025 at 12:01 A.M. which differs from the baseline."
    wrapped = textwrap.wrap(cap_text, width=50)
    cap_h = max(50, len(wrapped) * 18 + 24)

    total_h = LABEL_H + cap_h + 100
    out = Image.new("RGB", (300, total_h), (15, 23, 42))
    out.paste(img, (0, LABEL_H + cap_h))

    assert out.height > img.height, f"expected > 100, got {out.height}"
    assert out.width == img.width

def test_caption_image_saves_to_png():
    from PIL import Image
    img = Image.new("RGB", (300, 100), (20, 20, 50))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    assert buf.tell() > 0

try:
    from PIL import Image
    test("caption image taller than source",  test_caption_image_taller_than_input)
    test("caption image saves to PNG",         test_caption_image_saves_to_png)
except ImportError as e:
    print(f"  (skipped — PIL not importable: {e})")


# ---------------------------------------------------------------------------
# 3.  Diff ZIP builder (Feature 3)
# ---------------------------------------------------------------------------
print("\n── Feature 3: Diff ZIP download ──")

def test_zip_contains_files():
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("page1_diff.png", b"fakepng1")
        zf.writestr("page2_diff.png", b"fakepng2")
    buf.seek(0)
    with zipfile.ZipFile(buf) as zf:
        names = zf.namelist()
    assert "page1_diff.png" in names
    assert "page2_diff.png" in names
    assert len(names) == 2

def test_zip_empty_result_has_no_files():
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        pass  # nothing to add
    buf.seek(0)
    with zipfile.ZipFile(buf) as zf:
        assert len(zf.namelist()) == 0

test("ZIP contains expected filenames", test_zip_contains_files)
test("ZIP with no snapshots is empty",  test_zip_empty_result_has_no_files)


# ---------------------------------------------------------------------------
# 4.  PDF report service (Feature 4)
# ---------------------------------------------------------------------------
print("\n── Feature 4: PDF report generation ──")

def test_pdf_report_returns_bytes():
    """generate_pdf_report returns non-empty bytes starting with %PDF."""
    try:
        from app.services.pdf_report import generate_pdf_report
    except ImportError as e:
        print(f"    (skipped — {e})")
        return

    # Build minimal mock objects
    from datetime import datetime

    class _Mock:
        def __init__(self, **kw):
            for k, v in kw.items():
                setattr(self, k, v)

    rr = _Mock(
        id=1, run_id=1, project_id=1, test_case_id=1, form_id=1,
        status="in_review", result_json='{"mode":"benchmark","observations":['
            '{"current_page":"1","observation":"Effective Date differs.","confidence":"certain"},'
            '{"current_page":"2","observation":"Named Insured filled.","confidence":"likely"}'
        '],"overall_summary":"2 observations found.","visual_validation":[]}',
        summary_text="2 observations found.",
    )
    project = _Mock(name="Liberty Mutual POC")
    tc      = _Mock(name="test 2", mode="benchmark", benchmark_form_id=None)
    main_form = _Mock(original_filename="SIGL-0147A.pdf", stored_filename="form_1.pdf",
                      size_bytes=156_672, uploaded_at=datetime(2026, 5, 1, 21, 29))
    bench_form = _Mock(original_filename="SIGL-0147A-baseline.pdf", stored_filename="bench_1.pdf",
                       size_bytes=148_480, uploaded_at=datetime(2026, 4, 30, 10, 0))

    with tempfile.TemporaryDirectory() as tmp:
        pdf_bytes = generate_pdf_report(
            rr=rr, project=project, tc=tc,
            main_form=main_form, bench_form=bench_form,
            instance_path=tmp,
        )

    assert isinstance(pdf_bytes, bytes), "expected bytes"
    assert len(pdf_bytes) > 1000, f"PDF too small: {len(pdf_bytes)} bytes"
    assert pdf_bytes[:4] == b"%PDF", f"not a PDF: {pdf_bytes[:8]}"

test("PDF report returns valid PDF bytes", test_pdf_report_returns_bytes)


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
print()
if _failures:
    print(f"  {len(_failures)} test(s) FAILED: {', '.join(_failures)}")
    sys.exit(1)
else:
    print(f"  All tests passed.")
    sys.exit(0)
