"""
Standalone engine tests — no Flask, no real PDFs, no network calls.
Run with: python tests/test_engine.py
"""
import io
import json
import os
import sys
import types
import struct
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS = "\033[92m✓\033[0m"
FAIL = "\033[91m✗\033[0m"
_failures: list[str] = []


def test(name: str, fn):
    try:
        fn()
        print(f"  {PASS}  {name}")
    except Exception as e:
        print(f"  {FAIL}  {name}  —  {e}")
        _failures.append(name)


# ---------------------------------------------------------------------------
# 1. Config
# ---------------------------------------------------------------------------
print("\n── Config ──")

def test_llm_config_defaults():
    from config import LLMConfig
    c = LLMConfig()
    assert c.timeout_secs == 300
    assert c.max_retries == 3

def test_parser_config_defaults():
    from config import ParserConfig
    c = ParserConfig()
    assert c.scanned_word_threshold == 30
    assert c.render_dpi == 150

def test_diff_config_defaults():
    from config import DiffConfig
    c = DiffConfig()
    assert 0.99 < c.similarity_threshold < 1.0

test("LLMConfig defaults",    test_llm_config_defaults)
test("ParserConfig defaults", test_parser_config_defaults)
test("DiffConfig defaults",   test_diff_config_defaults)


# ---------------------------------------------------------------------------
# 2. Document parser data structures
# ---------------------------------------------------------------------------
print("\n── Document Parser ──")

def test_parsed_document_full_text():
    from engine.document_parser import ParsedDocument, PageContent
    pages = [
        PageContent(1, "Hello world.", [], {}),
        PageContent(2, "Second page.", [], {}),
    ]
    doc = ParsedDocument(pages=pages, page_count=2)
    text = doc.full_text()
    assert "Hello world" in text
    assert "Second page" in text
    assert "[Page 1]" in text

def test_parsed_document_all_form_fields_merge():
    from engine.document_parser import ParsedDocument, PageContent
    pages = [
        PageContent(1, "", [], {"Policy No": "123"}),
        PageContent(2, "", [], {"Effective Date": "01/01/2026"}),
    ]
    doc = ParsedDocument(pages=pages, page_count=2, form_fields={"Insured": "ACME"})
    fields = doc.all_form_fields()
    assert fields["Policy No"] == "123"
    assert fields["Effective Date"] == "01/01/2026"
    assert fields["Insured"] == "ACME"

def test_extract_markdown_tables():
    from engine.document_parser import _extract_markdown_tables
    md = """
| Name | Value | Notes |
|------|-------|-------|
| Premium | $1,200 | Annual |
| Deductible | $500 | Per claim |
"""
    tables = _extract_markdown_tables(md)
    assert len(tables) == 1
    assert tables[0]["headers"] == ["Name", "Value", "Notes"]
    assert len(tables[0]["rows"]) == 2

def test_extract_form_fields_from_markdown():
    from engine.document_parser import _extract_form_fields_from_markdown
    md = "Named Insured: ACME Corporation\nEffective Date: 01/01/2026\nPolicy Number: POL-9999"
    fields = _extract_form_fields_from_markdown(md)
    assert "Named Insured" in fields or "Effective Date" in fields

def test_detect_scan_sparse():
    from engine.document_parser import _detect_scan, PageContent
    from config import ParserConfig
    cfg = ParserConfig()
    sparse_pages = [PageContent(i, "few words", [], {}) for i in range(1, 4)]
    assert _detect_scan(sparse_pages, cfg) is True

def test_detect_scan_digital():
    from engine.document_parser import _detect_scan, PageContent
    from config import ParserConfig
    cfg = ParserConfig()
    rich_pages = [PageContent(i, "word " * 100, [], {}) for i in range(1, 4)]
    assert _detect_scan(rich_pages, cfg) is False

test("ParsedDocument.full_text()",         test_parsed_document_full_text)
test("ParsedDocument field merge",          test_parsed_document_all_form_fields_merge)
test("Markdown table extraction",           test_extract_markdown_tables)
test("Form field extraction from markdown", test_extract_form_fields_from_markdown)
test("Scan detection — sparse",            test_detect_scan_sparse)
test("Scan detection — digital",           test_detect_scan_digital)


# ---------------------------------------------------------------------------
# 3. OCR Manager
# ---------------------------------------------------------------------------
print("\n── OCR Manager ──")

def test_classify_page_digital():
    from engine.ocr_manager import classify_page, PageType
    assert classify_page("word " * 50) == PageType.DIGITAL

def test_classify_page_scanned():
    from engine.ocr_manager import classify_page, PageType
    assert classify_page("", image_ratio=0.9) == PageType.SCANNED

def test_classify_page_blank():
    from engine.ocr_manager import classify_page, PageType
    assert classify_page("") == PageType.BLANK

def test_classify_page_mixed():
    from engine.ocr_manager import classify_page, PageType
    assert classify_page("five words here now") == PageType.MIXED

def test_classify_document_digital():
    from engine.ocr_manager import classify_document, PageType
    texts = ["word " * 60] * 5
    assert classify_document(texts) == PageType.DIGITAL

def test_classify_document_scanned():
    from engine.ocr_manager import classify_document, PageType
    texts = [""] * 5
    assert classify_document(texts) == PageType.SCANNED

def test_normalize_insurance_text():
    from engine.ocr_manager import normalize_insurance_text
    result = normalize_insurance_text("eff. date: 01/01/2026   pol. no.: 123")
    assert "Effective Date" in result
    assert "Policy Number" in result

test("classify_page — digital",          test_classify_page_digital)
test("classify_page — scanned",          test_classify_page_scanned)
test("classify_page — blank",            test_classify_page_blank)
test("classify_page — mixed",            test_classify_page_mixed)
test("classify_document — digital",      test_classify_document_digital)
test("classify_document — scanned",      test_classify_document_scanned)
test("normalize_insurance_text",         test_normalize_insurance_text)


# ---------------------------------------------------------------------------
# 4. Semantic Diff
# ---------------------------------------------------------------------------
print("\n── Semantic Diff ──")

def _make_doc(pages_text: dict[int, str], fields: dict = None):
    from engine.document_parser import ParsedDocument, PageContent
    pages = [PageContent(pg, txt, [], {}) for pg, txt in pages_text.items()]
    return ParsedDocument(pages=pages, page_count=len(pages), form_fields=fields or {})

def test_diff_form_fields_modified():
    from engine.semantic_diff import diff_form_fields
    b = _make_doc({1: ""}, {"Effective Date": "01/01/2025", "Premium": "$1,200"})
    c = _make_doc({1: ""}, {"Effective Date": "10/15/2025", "Premium": "$1,200"})
    changes = diff_form_fields(b, c)
    assert len(changes) == 1
    assert changes[0].field_name == "Effective Date"
    assert changes[0].change_type == "modified"

def test_diff_form_fields_added():
    from engine.semantic_diff import diff_form_fields
    b = _make_doc({1: ""}, {})
    c = _make_doc({1: ""}, {"Watermark": "DRAFT"})
    changes = diff_form_fields(b, c)
    assert any(ch.change_type == "added" for ch in changes)

def test_diff_form_fields_removed():
    from engine.semantic_diff import diff_form_fields
    b = _make_doc({1: ""}, {"Signature": "John Smith"})
    c = _make_doc({1: ""}, {})
    changes = diff_form_fields(b, c)
    assert any(ch.change_type == "removed" for ch in changes)

def test_diff_form_fields_no_change():
    from engine.semantic_diff import diff_form_fields
    b = _make_doc({1: ""}, {"Name": "ACME"})
    c = _make_doc({1: ""}, {"Name": "ACME"})
    assert diff_form_fields(b, c) == []

def test_diff_page_text_modified():
    from engine.semantic_diff import diff_page_text
    changes = diff_page_text("The premium is $1,200.", "The premium is $1,500.", page=1)
    assert len(changes) >= 1
    assert any("modified" in ch.change_type for ch in changes)

def test_structured_diff_page_count():
    from engine.semantic_diff import build_structured_diff
    b = _make_doc({1: "page 1", 2: "page 2"})
    c = _make_doc({1: "page 1"})
    sd = build_structured_diff(b, c)
    assert sd.page_count_changed is True
    assert sd.baseline_pages == 2
    assert sd.current_pages == 1

def test_structured_diff_empty():
    from engine.semantic_diff import build_structured_diff
    b = _make_doc({1: "identical text"}, {"Field": "value"})
    c = _make_doc({1: "identical text"}, {"Field": "value"})
    sd = build_structured_diff(b, c)
    assert sd.is_empty()

def test_fallback_observations():
    from engine.semantic_diff import _fallback_observations, StructuredDiff, FieldChange
    sd = StructuredDiff(
        field_changes=[FieldChange("Eff Date", "01/01/2025", "10/15/2025", change_type="modified")]
    )
    obs = _fallback_observations(sd)
    assert len(obs) >= 1
    assert "Eff Date" in obs[0]["observation"]

test("diff_form_fields — modified",     test_diff_form_fields_modified)
test("diff_form_fields — added",        test_diff_form_fields_added)
test("diff_form_fields — removed",      test_diff_form_fields_removed)
test("diff_form_fields — no change",    test_diff_form_fields_no_change)
test("diff_page_text — modified",       test_diff_page_text_modified)
test("StructuredDiff — page count",     test_structured_diff_page_count)
test("StructuredDiff — empty",          test_structured_diff_empty)
test("Fallback observations",           test_fallback_observations)


# ---------------------------------------------------------------------------
# 5. Vision Pipeline
# ---------------------------------------------------------------------------
print("\n── Vision Pipeline ──")

def test_align_pages_equal():
    from engine.vision_pipeline import align_pages, PageImage
    base = [PageImage(i, "b64", "image/jpeg", 100, 100) for i in range(1, 4)]
    curr = [PageImage(i, "b64", "image/jpeg", 100, 100) for i in range(1, 4)]
    aligned = align_pages(base, curr)
    assert len(aligned) == 3
    assert all(p["op"] == "matched" for p in aligned)

def test_align_pages_inserted():
    from engine.vision_pipeline import align_pages, PageImage
    base = [PageImage(i, "b64", "image/jpeg", 100, 100) for i in range(1, 3)]  # 2 pages
    curr = [PageImage(i, "b64", "image/jpeg", 100, 100) for i in range(1, 4)]  # 3 pages
    aligned = align_pages(base, curr)
    ops = [p["op"] for p in aligned]
    assert "inserted" in ops

def test_align_pages_deleted():
    from engine.vision_pipeline import align_pages, PageImage
    base = [PageImage(i, "b64", "image/jpeg", 100, 100) for i in range(1, 4)]  # 3 pages
    curr = [PageImage(i, "b64", "image/jpeg", 100, 100) for i in range(1, 3)]  # 2 pages
    aligned = align_pages(base, curr)
    ops = [p["op"] for p in aligned]
    assert "deleted" in ops

def test_merge_observations_dedup():
    from engine.vision_pipeline import merge_observations
    sem = [{"current_page": "1", "observation": "Effective Date changed from X to Y", "confidence": "certain"}]
    vis = [
        {"current_page": "1", "observation": "Effective Date value changed", "confidence": "likely"},     # dup
        {"current_page": "2", "observation": "Watermark DRAFT added to page 2", "confidence": "certain"}, # new
    ]
    merged = merge_observations(sem, vis)
    # Should have 2: original semantic + new watermark, not 3
    assert len(merged) == 2
    assert any("Watermark" in o["observation"] for o in merged)

def test_merge_observations_source_tag():
    from engine.vision_pipeline import merge_observations
    sem = [{"current_page": "1", "observation": "Field X changed", "confidence": "certain"}]
    vis = [{"current_page": "3", "observation": "Logo removed from footer", "confidence": "likely"}]
    merged = merge_observations(sem, vis)
    vision_obs = [o for o in merged if o.get("source") == "vision"]
    assert len(vision_obs) == 1

test("align_pages — equal count",    test_align_pages_equal)
test("align_pages — inserted page",  test_align_pages_inserted)
test("align_pages — deleted page",   test_align_pages_deleted)
test("merge_observations — dedup",   test_merge_observations_dedup)
test("merge_observations — source",  test_merge_observations_source_tag)


# ---------------------------------------------------------------------------
# 6. LLM Client (mock provider)
# ---------------------------------------------------------------------------
print("\n── LLM Client ──")

def test_run_llm_unknown_provider():
    from engine.llm_client import run_llm
    result = run_llm([{"role": "user", "content": "test"}], provider="nonexistent_xyz")
    assert "error" in result

def test_run_llm_empty_messages():
    from engine.llm_client import run_llm
    result = run_llm([{"role": "user", "content": ""}])
    assert "error" in result

def test_parse_json_with_fences():
    from engine.llm_client import _parse_json
    raw = '```json\n{"observations": [{"page": "1"}]}\n```'
    result = _parse_json(raw)
    assert "observations" in result

def test_parse_json_embedded():
    from engine.llm_client import _parse_json
    raw = 'Some text before {"key": "value"} and after'
    result = _parse_json(raw)
    assert result.get("key") == "value"

def test_make_llm_fn_returns_callable():
    from engine.llm_client import make_llm_fn
    fn = make_llm_fn(provider="openai")
    assert callable(fn)

test("run_llm — unknown provider",     test_run_llm_unknown_provider)
test("run_llm — empty messages",       test_run_llm_empty_messages)
test("_parse_json — fenced block",     test_parse_json_with_fences)
test("_parse_json — embedded JSON",    test_parse_json_embedded)
test("make_llm_fn — returns callable", test_make_llm_fn_returns_callable)


# ---------------------------------------------------------------------------
# 7. Pipeline helpers
# ---------------------------------------------------------------------------
print("\n── Pipeline helpers ──")

def test_accuracy_score_no_obs():
    from engine.pipeline import _accuracy_score
    assert _accuracy_score([]) == 100

def test_accuracy_score_certain():
    from engine.pipeline import _accuracy_score
    obs = [{"confidence": "certain"}] * 5
    score = _accuracy_score(obs)
    assert score == 50   # 100 - (5 * 10)

def test_accuracy_score_floor():
    from engine.pipeline import _accuracy_score
    obs = [{"confidence": "certain"}] * 20
    assert _accuracy_score(obs) == 0

def test_truncate_short():
    from engine.pipeline import _truncate
    assert _truncate("hello", 100) == "hello"

def test_truncate_long():
    from engine.pipeline import _truncate
    result = _truncate("x" * 200, 100)
    assert len(result) > 100   # includes truncation notice
    assert "truncated" in result

def test_parse_page_ref():
    from engine.pipeline import _parse_page_ref
    assert _parse_page_ref("1") == 1
    assert _parse_page_ref("2-3") == 2
    assert _parse_page_ref("all") == 0
    assert _parse_page_ref("") == 0

def test_schema_has_required_keys():
    from engine.pipeline import _schema
    s = _schema("benchmark")
    required = ["mode", "observations", "overall_summary", "visual_validation",
                "accuracy_score", "error", "engine_version"]
    for key in required:
        assert key in s, f"Missing key: {key}"

test("_accuracy_score — no obs",       test_accuracy_score_no_obs)
test("_accuracy_score — 5 certain",    test_accuracy_score_certain)
test("_accuracy_score — floor 0",      test_accuracy_score_floor)
test("_truncate — short",              test_truncate_short)
test("_truncate — long",               test_truncate_long)
test("_parse_page_ref",                test_parse_page_ref)
test("_schema — required keys",        test_schema_has_required_keys)


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
