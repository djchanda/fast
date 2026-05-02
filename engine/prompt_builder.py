from typing import Optional, Literal, Dict, Any, List

ValidationMode = Literal["basic", "specific", "benchmark"]


def _format_visual_diffs_for_llm(visual_diffs: list, extra_context: Optional[Dict[str, Any]] = None) -> str:
    """Format visual diff rows plus document-level signals into a readable LLM prompt block."""
    if not visual_diffs and not extra_context:
        return "No visual diff data available."

    lines = []

    for v in visual_diffs:
        page = v.get("page", "?")
        _sim = v.get("similarity")
        sim = float(_sim) if _sim is not None else 1.0
        diff_pct = v.get("diff_pixels_pct") or 0.0
        op = v.get("alignment_op", "matched")
        # Skip single-panel entries (fallback render_pages output — no comparison data)
        if op == "single":
            continue
        sig_candidate = v.get("signature_candidate", False)
        sig_label = v.get("signature_label", "")
        zone = v.get("zone_analysis") or {}
        changed_zones = zone.get("changed_zones", [])
        change_pattern = zone.get("change_pattern", "")
        change_hint = zone.get("change_hint", "")

        if op == "deleted":
            lines.append(f"  Page {page}: DELETED — entire page removed from current PDF.")
            continue
        if op == "inserted":
            is_blank = v.get("is_blank_page", False)
            if is_blank:
                lines.append(
                    f"  Page {page}: INSERTED (is_blank_page=true) — blank/empty page added. "
                    f"Likely an intentional separator. Low severity."
                )
            else:
                lines.append(
                    f"  Page {page}: INSERTED (is_blank_page=false) — new content page with no baseline counterpart."
                )
            continue

        has_text_changes = v.get("has_text_changes", False)
        has_fmt = v.get("has_formatting_changes", False)
        formatting_summary = v.get("formatting_summary", "")
        line_changes = v.get("changes", [])

        status = "MAJOR" if v.get("major") else ("WARN" if v.get("warn") else "OK")
        header = f"  Page {page}: {status} | similarity={sim:.3f} ({diff_pct:.1f}% pixels differ)"
        parts = [header]

        if line_changes:
            parts.append("LINE DIFF (line-by-line comparison):")
            for c in line_changes[:20]:
                ct = c.get("type", "")
                if ct == "deleted":
                    parts.append(f'    REMOVED: "{c["exp_text"][:120]}"')
                elif ct == "inserted":
                    parts.append(f'    ADDED:   "{c["act_text"][:120]}"')
                elif ct == "modified":
                    parts.append(
                        f'    CHANGED: was="{c["exp_text"][:80]}"'
                        f'\n             now="{c["act_text"][:80]}"'
                    )
                elif ct == "replaced_block":
                    parts.append(
                        f'    BLOCK WAS: "{c["exp_text"][:100]}"'
                        f'\n    BLOCK NOW: "{c["act_text"][:100]}"'
                    )
        elif not has_text_changes and not has_fmt:
            if v.get("major") or v.get("warn"):
                parts.append("TEXT DIFF: Content identical — pixel differences are visual/graphical noise")

        bold_changed = v.get("bold_changed", [])
        size_changed = v.get("size_changed", [])
        list_align = v.get("list_alignment_shifted", [])
        if bold_changed:
            parts.append("BOLD CHANGED: " + ", ".join(repr(t) for t in bold_changed[:6]))
        if size_changed:
            parts.append("FONT SIZE CHANGED: " + ", ".join(size_changed[:6]))
        if list_align:
            parts.append("LIST ALIGNMENT SHIFTED: " + ", ".join(repr(t) for t in list_align[:6]))
        elif formatting_summary and not bold_changed and not size_changed and not list_align:
            parts.append(f"FORMATTING: {formatting_summary}")

        graphics_diff = v.get("graphics_diff", "")
        if graphics_diff:
            parts.append(f"GRAPHICS CHANGE: {graphics_diff}")

        if sig_candidate:
            parts.append(f"[SIGNATURE CANDIDATE near '{sig_label}']")

        lines.append("\n".join(parts))

    # Document-level signals from metadata and field structure comparison
    if extra_context:
        metadata_diff = extra_context.get("metadata_diff") or {}
        field_diff = extra_context.get("field_diff") or {}

        doc_parts = []
        if metadata_diff.get("has_metadata_changes"):
            doc_parts.append("METADATA CHANGES (document properties):")
            for k, v in (metadata_diff.get("changed") or {}).items():
                doc_parts.append(f"  {k}: '{v['expected']}' → '{v['actual']}'")

        if field_diff.get("has_structural_changes"):
            doc_parts.append("FORM FIELD STRUCTURE CHANGES:")
            if field_diff.get("removed_fields"):
                doc_parts.append("  REMOVED FIELDS: " + ", ".join(field_diff["removed_fields"]))
            if field_diff.get("added_fields"):
                doc_parts.append("  ADDED FIELDS: " + ", ".join(field_diff["added_fields"]))
            if field_diff.get("changed_fields"):
                for cf in field_diff["changed_fields"][:5]:
                    doc_parts.append(
                        f"  FIELD TYPE CHANGED: '{cf['name']}' "
                        f"{cf['expected_type']} → {cf['actual_type']}"
                    )

        if doc_parts:
            lines.append("\nDOCUMENT-LEVEL CHANGES:\n" + "\n".join(doc_parts))

    return "\n\n".join(lines) if lines else "No visual diff data available."


def build_prompt(
    mode: ValidationMode,
    current_doc: Dict[str, Any],
    benchmark_doc: Optional[Dict[str, Any]],
    base_prompt: str,
    extra_context: Optional[Dict[str, Any]] = None,
    baseline_images: Optional[List[Dict[str, Any]]] = None,
    current_images: Optional[List[Dict[str, Any]]] = None,
) -> List[dict]:
    """
    Returns messages list suitable for OpenAI-style chat format.

    Supports all three validation modes with comprehensive mode-specific
    validation rules covering the full FAST feature specification.
    """

    current_text = current_doc.get("text", "")
    current_fields = current_doc.get("fields", {})
    current_pages = current_doc.get("pages", [])
    current_meta = current_doc.get("meta", {})
    current_visual_diffs = current_doc.get("visual_diffs", [])

    benchmark_text = ""
    benchmark_fields = {}
    benchmark_pages = []
    benchmark_meta = {}

    if benchmark_doc:
        benchmark_text = benchmark_doc.get("text", "")
        benchmark_fields = benchmark_doc.get("fields", {})
        benchmark_pages = benchmark_doc.get("pages", [])
        benchmark_meta = benchmark_doc.get("meta", {})

    system_msg = {
        "role": "system",
        "content": (
            "You are an expert PDF form validator for financial and insurance documents.\n"
            "You ALWAYS return STRICT JSON with the schema requested. No commentary outside JSON.\n\n"

            # ── OCR & watermark rules (unchanged) ──────────────────────────
            "GENERAL RULES:\n"
            "- Some PDFs are scanned; extracted text may come from OCR. OCR can introduce noise.\n"
            "- Prefer form fields where available; otherwise rely on extracted text.\n"
            "- If per-page text is provided, use it for correct page numbers.\n\n"

            "OCR TEXT RELIABILITY — CRITICAL (prevents false positives):\n"
            "- The extraction meta includes 'is_scanned_like', 'used_ocr', and "
            "'text_is_sparse' flags.\n"
            "- If is_scanned_like=True OR used_ocr=True OR text_is_sparse=True: body text "
            "came from OCR or a minimal searchable layer — it is UNRELIABLE for spell-checking.\n"
            "- For SCANNED/SPARSE PDFs: do NOT report single-word or single-character spelling "
            "errors from body text — these are OCR artifacts, not real typos.\n"
            "- For SCANNED/SPARSE PDFs: only report a spelling error if it ALSO appears verbatim "
            "in a form field value (AcroForm data is always reliable).\n"
            "- Common OCR false positives to NEVER report: single letters swapped, 'rn' "
            "read as 'm', 'vv' read as 'w', character-level noise in legal/technical terms.\n\n"

            "CONSERVATIVE SPELL-CHECK RULES — applies to ALL PDFs regardless of scan status:\n"
            "Insurance, legal, and financial forms are professionally authored. Genuine spelling "
            "errors are extremely rare. Apply ALL of these tests before reporting any spelling error:\n"
            "  1. The word must be a COMMON everyday word (not a legal term, insurance term, "
            "Latin phrase, medical term, or industry abbreviation).\n"
            "  2. The error must be OBVIOUS to a non-specialist — clear transposition of ≤3 chars "
            "in a short common word (e.g. 'teh'→'the', 'recieve'→'receive').\n"
            "  3. The word is NOT in ALL-CAPS (all-caps text is intentional style in legal headers).\n"
            "  4. The same word does NOT appear consistently throughout the document "
            "(consistent = intentional spelling or house style).\n"
            "  5. The error is in a user-visible label or section header — NOT inside legal "
            "definitions, boilerplate clauses, or legal disclaimers (those have specialised "
            "vocabulary that must not be second-guessed).\n"
            "NEVER report as spelling errors:\n"
            "  - Any word longer than 8 characters with a single-character difference "
            "(rendering artifact, not a typo).\n"
            "  - Legal definitions, disclosure text, or statutory language.\n"
            "  - Proper nouns, company names, policy codes, form numbers.\n"
            "  - Words inside parentheses in legal clauses.\n"
            "  - Text that could be an abbreviation or acronym.\n\n"

            "PLACEHOLDER DETECTION — NARROW SCOPE:\n"
            "- Only flag as placeholder test data if the value is a widely-known generic "
            "placeholder: 'Lorem ipsum', 'TEST DATA', 'SAMPLE', 'TBD', 'Enter text here', "
            "'N/A', '[INSERT NAME]', etc.\n"
            "- Do NOT flag system identifiers, form codes, internal IDs, version numbers, "
            "or application-specific tokens (e.g. 'If360_Testing', 'POL-DRAFT-001') as "
            "placeholder data — these are legitimate technical identifiers.\n\n"

            "WATERMARK / SPECIMEN ARTIFACT DETECTION:\n"
            "- Insurance forms frequently carry diagonal 'SAMPLE', 'SPECIMEN', 'DRAFT', 'VOID', "
            "'NOT FOR SALE', 'COPY' stamps across the page.\n"
            "- Scattered isolated single characters or short fragments (e.g. 'S', 'A', 'M', "
            "'P', 'L', 'E' spread across a page) are parts of a watermark rendered as text — "
            "they are NOT individual spelling errors or data errors.\n"
            "- NEVER report watermark character fragments as spelling_errors — they are artifacts.\n"
            "- If a watermark is detected, report it ONCE as a single format_issue with "
            "severity='low' and description 'Watermark stamp detected (SAMPLE/SPECIMEN/etc.)'.\n"
            "- If the same watermark repeats across all pages, report it once total — not per page.\n"
            "- Do NOT count watermark fragments toward the spelling_errors summary count.\n\n"

            # ── MODE 1 — BASIC validation rules ───────────────────────────
            "MODE 1 — BASIC VALIDATION TARGETS:\n\n"

            "SPELLING & LANGUAGE (report as spelling_errors or format_issues):\n"
            "- Spell-check field labels, section headers, instructions, and helper text.\n"
            "- Do NOT spell-check legal disclaimers, legal definitions, boilerplate clauses, "
            "or statutory text — these use specialised vocabulary and must not be questioned.\n"
            "- Apply the CONSERVATIVE SPELL-CHECK RULES above before reporting anything.\n"
            "- Detect placeholder text left in production: patterns like 'Enter…', 'Type here…', "
            "'Lorem ipsum', 'TBD', 'Sample', 'Test', 'N/A' in unexpected locations → "
            "format_issue severity=high.\n"
            "- Detect duplicate field labels on the same page → format_issue severity=medium.\n"
            "- Detect inconsistent capitalization: Title Case vs ALL CAPS vs Sentence case "
            "within the same label category → format_issue severity=low.\n"
            "- Detect inconsistent punctuation on labels: 'First Name:' vs 'Last Name' (colon "
            "missing) within the same form → format_issue severity=low.\n"
            "- Detect truncated text cut off at field boundaries → layout_anomaly severity=medium.\n"
            "- Detect field number sequences that skip values (e.g. 4 → 6) → format_issue severity=low.\n\n"

            "TYPOGRAPHY CONSISTENCY (report as typography_issues or layout_anomalies):\n"
            "- Detect field labels using a font size >2pt different from the majority → "
            "typography_issue severity=medium, property=font_size.\n"
            "- Detect field labels using a different font family than all other labels → "
            "typography_issue severity=high, property=font_family.\n"
            "- Detect section headers not bold when all other headers are bold → "
            "typography_issue severity=medium, property=bold.\n"
            "- Detect body text that is bold when no other body text is bold → "
            "typography_issue severity=low, property=bold.\n"
            "- Detect italic or underline usage inconsistent within a text category → "
            "typography_issue severity=low.\n"
            "- Flag text below 8pt as readability concern → compliance_issue severity=low, "
            "standard='WCAG21 1.4.4'.\n"
            "- Flag text below 6pt as critical readability issue → compliance_issue severity=high.\n\n"

            "LAYOUT & SPATIAL CONSISTENCY (report as layout_anomalies):\n"
            "- Detect fields visually misaligned relative to their row/column group → "
            "layout_anomaly severity=medium.\n"
            "- Detect fields significantly wider/narrower than similar fields (>15% deviation) → "
            "layout_anomaly severity=low.\n"
            "- Detect elements that appear to overlap → layout_anomaly severity=high.\n"
            "- Detect elements extending beyond page margin → layout_anomaly severity=medium.\n"
            "- Detect header/footer elements that shift position across pages → "
            "layout_anomaly severity=medium.\n"
            "- Detect uneven vertical spacing between sections → layout_anomaly severity=low.\n\n"

            "FORM COMPLETENESS (report as missing_content):\n"
            "- Detect required fields (asterisk or 'Required' label) that appear empty → "
            "missing_content severity=critical.\n"
            "- Detect form sections referencing attachments/exhibits not present → "
            "missing_content severity=high.\n"
            "- Detect broken or missing page number sequence → missing_content severity=medium.\n"
            "- Detect missing form version number or revision date in footer → "
            "missing_content severity=low.\n"
            "- Detect missing form title on page 1 → missing_content severity=medium.\n"
            "- Detect missing legal disclaimer or consent block when expected for the form type → "
            "missing_content severity=high.\n"
            "- Detect empty signature block in a form that appears to be filled → "
            "missing_content severity=critical.\n"
            "- Detect empty date field adjacent to a signature block → "
            "missing_content severity=high.\n\n"

            "ACCESSIBILITY & USABILITY (report as accessibility_issues or compliance_issues):\n"
            "- Detect apparent low contrast (very light text on light background) → "
            "compliance_issue severity=medium, standard='WCAG21 1.4.3'.\n"
            "- Detect form fields with no visible label (orphan inputs) → "
            "accessibility_issue severity=high, type='missing_label'.\n"
            "- Detect interactive groups (radio, checkbox) with no group label → "
            "accessibility_issue severity=medium.\n"
            "- Detect tables with no visible column headers → "
            "accessibility_issue severity=medium.\n"
            "- Detect color used as the only means to convey information → "
            "compliance_issue severity=medium, standard='WCAG21 1.4.1'.\n"
            "- Detect form sections with no header or section label → "
            "accessibility_issue severity=low.\n\n"

            # ── MODE 2 — SPECIFIC assertion rules ────────────────────────
            "MODE 2 — SPECIFIC VALIDATION — ASSERTION TYPES:\n"
            "Execute each user-provided assertion and place results in the appropriate category.\n\n"
            "- EXACT VALUE: field must match expected string exactly → "
            "value_mismatch if fail (set expected= and actual= fields).\n"
            "- PATTERN/FORMAT: field must match date format (MM/DD/YYYY, DD/MM/YYYY, YYYY-MM-DD etc.), "
            "regex, email, phone, postal code, or currency → format_issue if fail.\n"
            "- DATE RANGE: date must be within specified range → format_issue if fail.\n"
            "- SIGNATURE: block must be signed / non-empty / have date → missing_content if fail.\n"
            "- CHECKBOX/SELECTION: specific box must be checked/unchecked; dropdown must match → "
            "value_mismatch if fail.\n"
            "- CONDITIONAL: IF field A = X THEN field B must be Y → compliance_issue if fail.\n"
            "- CALCULATION: field must equal computed formula (sum, product, percentage) → "
            "value_mismatch if fail (show expected=calculated and actual=found).\n"
            "- Report each failed assertion as a separate finding. For PASSES with high business "
            "importance, add a low-severity format_issue noting pass so the tester has a full audit trail.\n\n"

            # ── MODE 3 — BENCHMARK change categories ─────────────────────
            "MODE 3 — BENCHMARK VALIDATION — CHANGE CATEGORIES:\n\n"

            "CONTENT CHANGES (value_mismatches / missing_content / extra_content):\n"
            "- Any word or phrase changed in labels, headers, instructions, legal text → "
            "value_mismatch severity=high (show was/now).\n"
            "- Entire line of text added → extra_content.\n"
            "- Entire line of text removed → missing_content.\n"
            "- Field label or section title renamed → value_mismatch severity=high.\n"
            "- Footer text changed (version number, date, org name) → format_issue severity=medium.\n\n"

            "TYPOGRAPHY CHANGES (typography_issues / format_issues):\n"
            "- Font size changed on any element → typography_issue severity=medium, "
            "property=font_size (show old_pt → new_pt).\n"
            "- Font family changed → typography_issue severity=high, property=font_family.\n"
            "- Bold added or removed → typography_issue severity=medium, property=bold.\n"
            "- Italic/underline/strikethrough toggled → typography_issue severity=low.\n"
            "- Text color changed → typography_issue severity=medium, property=color.\n"
            "- Text alignment changed (left→center etc.) → layout_anomaly severity=medium.\n\n"

            "STRUCTURAL CHANGES — use FORM FIELD STRUCTURE signal (missing_content / extra_content):\n"
            "- REMOVED FIELDS in signal → missing_content severity=critical for each.\n"
            "- ADDED FIELDS in signal → extra_content severity=high for each.\n"
            "- Field type changed (editable→read-only or vice versa) → "
            "visual_mismatch severity=high, category='Field property'.\n"
            "- Section added → extra_content severity=critical.\n"
            "- Section removed → missing_content severity=critical.\n"
            "- Required field indicator added/removed → format_issue severity=high.\n"
            "- Signature block added/removed → extra_content/missing_content severity=critical.\n\n"

            "METADATA & INVISIBLE CHANGES — use METADATA signal (format_issues / visual_mismatches):\n"
            "- Form version or revision date in footer changed → format_issue severity=medium.\n"
            "- PDF metadata (Title, Author, Subject, Keywords) changed → "
            "visual_mismatch severity=low, category='Metadata'.\n"
            "- PDF security settings changed → visual_mismatch severity=medium, category='Metadata'.\n\n"

            "FORMATTING & STYLE CHANGES (visual_mismatches):\n"
            "- Field border style/weight/color changed → visual_mismatch severity=low.\n"
            "- Background color changed → visual_mismatch severity=medium.\n"
            "- Logo image replaced or resized → visual_mismatch severity=high.\n"
            "- Watermark added, removed, or changed → visual_mismatch severity=medium.\n\n"

            "BENCHMARK SEVERITY TABLE:\n"
            "- Field/section/page removed: critical\n"
            "- Field/section/page added: high\n"
            "- Content changed (value, label, clause): high\n"
            "- Typography changed (font, bold, size): medium\n"
            "- Style changed (border, color, logo): low\n"
            "- Metadata changed: low–medium\n\n"

            # ── Formatting diff signal (unchanged) ────────────────────────
            "FORMATTING CHANGES FROM VISUAL DIFF SIGNALS:\n"
            "- BOLD CHANGED: text that flipped between bold and non-bold → format_issue. "
            "Severity: 'medium' for headings/titles, 'low' for body text.\n"
            "- FONT SIZE CHANGED: text whose point size changed → format_issue or typography_issue.\n"
            "- LIST ALIGNMENT SHIFTED: list markers (a), (b), 1., i. that moved → "
            "ALWAYS medium-severity layout_anomaly in legal/insurance documents.\n\n"

            "LINE DIFF — the authoritative accuracy signal (read this first):\n"
            "- REMOVED: line exists in baseline but not in current.\n"
            "- ADDED: line exists in current but not in baseline.\n"
            "- CHANGED: was='...' now='...' — always report as: "
            "\"Value changed from '<was>' to '<now>'\".\n"
            "- If LINE DIFF is absent and text is identical: no text changes. "
            "Only report visual/graphics changes if GRAPHICS CHANGE is present.\n"
            "- Dates, amounts, names, policy numbers in CHANGED lines → value_mismatches.\n\n"

            "TESTER PERSPECTIVE:\n"
            "- Real defects: wrong values, missing clauses, missing signatures, structural removals.\n"
            "- Not defects: minor layout pixel shifts, DPI noise, watermark rendering artefacts.\n"
            "- Only report alignment changes affecting ≥3 elements that are visually obvious.\n\n"

            "VALUE CHANGE FORMAT (critical rule):\n"
            "- Always: 'Changed from \"OLD\" to \"NEW\"' with exact quoted values.\n"
            "- For value_mismatches: expected=old value, actual=new value.\n\n"

            "BLANK / EMPTY INSERTED PAGES:\n"
            "- is_blank_page=true → low-severity extra_content, likely separator. Not critical.\n"
            "- is_blank_page=false → real content addition, medium/high.\n\n"

            "DETERMINISTIC REPORTING:\n"
            "1) Populate every top-level list even if empty.\n"
            "2) summary_counts must equal exact lengths of the arrays.\n"
            "3) pages_impacted = sorted unique page numbers from all findings.\n"
            "4) top_findings = up to 5 strongest findings.\n"
            "5) overall_summary based only on findings you output.\n"
            "6) Never say 'no differences' if any issue list has items.\n"
        ),
    }

    base_schema = r"""
Return ONLY valid JSON with this structure:

{
  "mode": "<basic|specific|benchmark>",

  "spelling_errors": [
    {"page": 1, "severity": "low", "text": "orig word", "suggestion": "correct word", "description": "brief note"}
  ],

  "format_issues": [
    {"page": 1, "severity": "low", "field_name": "date", "description": "Date is not in YYYY-MM-DD", "snippet": "12/05/25"}
  ],

  "value_mismatches": [
    {"page": 1, "severity": "critical", "field_name": "premium_amount", "expected": "100.00", "actual": "90.00", "description": "Value changed from \"100.00\" to \"90.00\""}
  ],

  "missing_content": [
    {"page": 3, "severity": "high", "field_name": "president_signature", "description": "President signature missing", "category": "Signature / approval block"}
  ],

  "extra_content": [
    {"page": 2, "severity": "medium", "field_name": "unexpected_section", "description": "Unexpected section present", "category": "Inserted content"}
  ],

  "layout_anomalies": [
    {"page": 2, "severity": "medium", "description": "Field misaligned or spacing inconsistent"}
  ],

  "typography_issues": [
    {"page": 1, "severity": "medium", "element": "Section header", "property": "font_size", "expected": "12pt", "actual": "8pt", "description": "Font size changed from 12pt to 8pt"}
  ],

  "structural_changes": [
    {"page": null, "severity": "critical", "element_type": "field", "change_type": "removed", "element_name": "EmergencyContactPhone", "description": "Form field 'EmergencyContactPhone' was removed"}
  ],

  "visual_mismatches": [
    {
      "page": 3,
      "severity": "high",
      "category": "Signature / approval block",
      "description": "Possible missing or changed signature near PRESIDENT",
      "evidence": "signature_candidate=true near PRESIDENT"
    }
  ],

  "compliance_issues": [
    {"page": 1, "severity": "medium", "standard": "WCAG21 1.4.3", "requirement": "Contrast ratio", "description": "Text contrast below 4.5:1"}
  ],

  "accessibility_issues": [
    {"page": 1, "severity": "high", "type": "missing_label", "field_name": "field_x", "description": "Form field has no accessible label"}
  ],

  "summary_counts": {
    "spelling_errors": 0,
    "format_issues": 0,
    "value_mismatches": 0,
    "missing_content": 0,
    "extra_content": 0,
    "layout_anomalies": 0,
    "typography_issues": 0,
    "structural_changes": 0,
    "visual_mismatches": 0,
    "compliance_issues": 0,
    "accessibility_issues": 0,
    "total": 0
  },

  "pages_impacted": [1, 2, 3],

  "top_findings": [
    {"severity": "critical", "page": 4, "category": "Value", "short": "Minimum premium changed from 1000 to 0"}
  ],

  "overall_summary": "Found <total> issue(s) across <n> page(s).",
  "accuracy_score": 0,

  "confidence_scores": {
    "spelling_errors": 0.95,
    "format_issues": 0.90,
    "value_mismatches": 0.98,
    "missing_content": 0.85,
    "extra_content": 0.80,
    "layout_anomalies": 0.75,
    "visual_mismatches": 0.92,
    "overall": 0.88
  }
}
"""

    summary_enforcement = """
Before writing the final JSON:
- Compute summary_counts from the exact lengths of all arrays.
- Build pages_impacted from all page numbers found in all finding arrays.
- Build top_findings using the strongest findings first (critical > high > medium > low).
- Write overall_summary LAST.
"""

    if mode == "basic":
        is_scanned = bool(
            (current_meta or {}).get("is_scanned_like")
            or (current_meta or {}).get("used_ocr")
            or (current_meta or {}).get("text_is_sparse")
        )
        scanned_notice = (
            "\n⚠ SCANNED/OCR/SPARSE-TEXT PDF DETECTED "
            "(is_scanned_like=True, used_ocr=True, or text_is_sparse=True):\n"
            "- Body text is OCR-extracted or from a minimal text layer — UNRELIABLE for spelling.\n"
            "- Do NOT report ANY spelling errors from body text — they are OCR artifacts.\n"
            "- ONLY report spelling errors found verbatim in AcroForm field values.\n"
            "- Focus instead on: Form Completeness, Accessibility, Layout, and Typography.\n"
            if is_scanned
            else (
                "\nPDF source: embedded text (avg_words_per_page="
                f"{(current_meta or {}).get('avg_words_per_page', '?')}).\n"
                "Apply CONSERVATIVE SPELL-CHECK RULES from the system message. "
                "Only report common-word errors with high confidence. "
                "Do NOT spell-check legal definitions, disclaimers, or boilerplate.\n"
            )
        )
        spell_instruction = (
            "For this scanned/OCR PDF: check AcroForm FIELDS ONLY for spelling "
            "(body text is unreliable OCR). Detect placeholders (narrow scope — see system rules)."
            if is_scanned
            else (
                "Apply CONSERVATIVE spell-check: common-word errors ONLY in field labels and "
                "section headers. Do NOT spell-check legal definitions, clauses, or disclaimers. "
                "Detect placeholders (narrow scope), duplicate labels, and obvious punctuation "
                "inconsistencies in labels."
            )
        )
        user_content = f"""
Perform BASIC VALIDATION on the following PDF form.
{scanned_notice}
Apply ALL five validation categories from the system rules:
1. SPELLING & LANGUAGE — {spell_instruction}
2. TYPOGRAPHY CONSISTENCY — Detect font size outliers, font family deviations, bold/italic inconsistencies, text below minimum readable size.
3. LAYOUT & SPATIAL CONSISTENCY — Detect field misalignment, overlapping elements, margin violations, header/footer shifts.
4. FORM COMPLETENESS — Detect empty required fields, missing form title/version, missing signature/date blocks, missing legal disclaimers.
5. ACCESSIBILITY & USABILITY — Detect low contrast, orphan input fields, tables without headers, color-only indicators.

There is NO benchmark document in this mode — validate the form against itself.

Current PDF extraction meta:
{current_meta}

Current PDF text (first 5000 chars):
{str(current_text or "")[:5000]}

Current PDF per-page text (first 3000 chars):
{str(current_pages or "")[:3000]}

Current PDF form fields (ALWAYS RELIABLE — use for value checks):
{current_fields}

{summary_enforcement}

{base_schema}
"""

    elif mode == "specific":
        is_scanned = bool(
            (current_meta or {}).get("is_scanned_like")
            or (current_meta or {}).get("used_ocr")
            or (current_meta or {}).get("text_is_sparse")
        )
        scanned_notice = (
            "\n⚠ SCANNED/OCR/SPARSE-TEXT PDF detected:\n"
            "- Body text is unreliable (OCR or minimal text layer). "
            "Do NOT report spelling errors from body text.\n"
            "- Only validate form field values against the user's assertions.\n"
            if is_scanned else ""
        )
        user_content = f"""
Perform SPECIFIC VALIDATION according to the user's rules.
{scanned_notice}
STRICT SCOPE — CRITICAL:
Only execute the assertions listed by the user below. Do NOT:
- Perform general spell-checking unless the user explicitly requested it.
- Report typography, layout, or accessibility issues unless the user asked for them.
- Add any findings beyond what the user's rules directly require.
Every finding you report MUST map to a specific user assertion or rule below.

User-provided validation instructions:
{base_prompt}

Execute each assertion using the appropriate type (Exact Value, Pattern/Format, Date Range,
Signature, Checkbox/Selection, Conditional, Calculation). Place results in the correct category.

Current PDF extraction meta:
{current_meta}

Current PDF text (first 5000 chars):
{str(current_text or "")[:5000]}

Current PDF per-page text (first 3000 chars):
{str(current_pages or "")[:3000]}

Current PDF form fields (ALWAYS RELIABLE — use for value assertions):
{current_fields}

{summary_enforcement}

{base_schema}
"""

    else:
        # benchmark mode
        formatted_visual_diffs = _format_visual_diffs_for_llm(current_visual_diffs, extra_context)

        # OCR detection: flag if either the baseline or current PDF is scanned
        is_scanned = bool(
            (benchmark_meta or {}).get("is_scanned_like")
            or (benchmark_meta or {}).get("used_ocr")
            or (current_meta or {}).get("is_scanned_like")
            or (current_meta or {}).get("used_ocr")
        )
        scanned_notice = (
            "\n⚠ SCANNED/OCR PDF DETECTED (is_scanned_like=True or used_ocr=True):\n"
            "- Body text in one or both PDFs is OCR-extracted and UNRELIABLE for spell-checking.\n"
            "- Do NOT report single-word spelling errors from body text — these are OCR artifacts.\n"
            "- Only report as a content change if the same difference also appears in form field values.\n"
            "- Focus on structural changes, metadata, and explicit LINE DIFF entries (these are reliable).\n"
            if is_scanned
            else ""
        )

        # Build extra context narrative
        doc_level_note = scanned_notice
        if extra_context:
            md = extra_context.get("metadata_diff") or {}
            fd = extra_context.get("field_diff") or {}
            if md.get("has_metadata_changes"):
                doc_level_note += "\nMETADATA CHANGES detected — see DOCUMENT-LEVEL CHANGES section in visual diff."
            if fd.get("has_structural_changes"):
                doc_level_note += "\nFIELD STRUCTURE CHANGES detected — removed/added fields listed in DOCUMENT-LEVEL CHANGES."

        # Truncate large text blocks to prevent prompt overflow and LLM JSON
        # parse failures (Gemini returns truncated JSON when input is too long).
        _MAX_TEXT = 6000
        _MAX_PAGES_CHARS = 4000

        def _trunc(s: Any, n: int) -> str:
            t = str(s or "")
            return t[:n] + "\n[... truncated ...]" if len(t) > n else t

        def _trunc_pages(pages: Any, n: int) -> str:
            txt = str(pages or "")
            return txt[:n] + "\n[... truncated ...]" if len(txt) > n else txt

        user_content = f"""
Perform BENCHMARK VALIDATION comparing a GOLDEN baseline PDF against a CURRENT PDF.

Apply ALL six change categories from the system rules:
1. CONTENT CHANGES — word/phrase changes, line additions/removals, label renames.
2. TYPOGRAPHY CHANGES — font size/family changes, bold/italic/underline toggles, color changes.
3. LAYOUT & POSITIONAL CHANGES — field movement/resizing, reflow cascades (group by root cause).
4. STRUCTURAL CHANGES — use FORM FIELD STRUCTURE signal for added/removed fields; section/page additions/removals.
5. METADATA & INVISIBLE CHANGES — use METADATA signal for document properties and field type changes.
6. FORMATTING & STYLE CHANGES — border/color/logo/watermark changes, watermarks present/absent.

Use the LINE DIFF as the primary accuracy signal.
For CHANGED lines: report "Changed from '<was>' to '<now>'".
For STRUCTURAL and METADATA signals: translate each entry into a finding in the appropriate category.
Group reflow cascades as a single finding with affected element count and root cause.

PAGE COUNT & IMAGE PAIRING — CRITICAL RULES:
- The VISUAL DIFF SUMMARY already identifies DELETED and INSERTED pages using sequence alignment.
  Do NOT re-report page removals or insertions as your own findings — they are handled by the system.
- Page images are labeled "BASELINE page X matched to CURRENT page Y" where X and Y may differ
  when content has been redistributed across a different number of pages. A difference in page
  numbers does NOT mean a page was removed — it means content was reformatted.
- When comparing images labeled with different page numbers (e.g. "BASELINE page 4 matched to
  CURRENT page 3"), focus on WHAT CHANGED between those two pages, not on the numbering.
- Only report page-count findings if the VISUAL DIFF SUMMARY explicitly marks a page as DELETED.
{doc_level_note}

User-provided additional instructions:
{base_prompt}

--- BASELINE (GOLDEN) PDF ---
Extraction meta: {benchmark_meta}
Full text:
{_trunc(benchmark_text, _MAX_TEXT)}

Per-page text:
{_trunc_pages(benchmark_pages, _MAX_PAGES_CHARS)}

Form fields: {benchmark_fields}

--- CURRENT PDF ---
Extraction meta: {current_meta}
Full text:
{_trunc(current_text, _MAX_TEXT)}

Per-page text:
{_trunc_pages(current_pages, _MAX_PAGES_CHARS)}

Form fields: {current_fields}

--- VISUAL DIFF SUMMARY (page-by-page + document-level signals) ---
{formatted_visual_diffs}

{summary_enforcement}

{base_schema}
"""

    # For benchmark mode, attach rendered page images so the LLM can use vision
    # to catch watermarks, table structure, section ordering, and other layout
    # details that text extraction alone misses.
    user_content_block = _build_user_content_with_images(
        user_content, mode, baseline_images, current_images, current_visual_diffs
    )
    return [
        system_msg,
        {"role": "user", "content": user_content_block},
    ]


def _build_user_content_with_images(
    text_content: str,
    mode: str,
    baseline_images: Optional[List[Dict[str, Any]]],
    current_images: Optional[List[Dict[str, Any]]],
    visual_diffs: Optional[List[Dict[str, Any]]] = None,
) -> Any:
    """
    Return a list of content blocks (text + images) when images are provided for
    benchmark mode, or the plain text string otherwise.

    Images are paired according to the sequence-alignment data (visual_diffs) so
    the LLM sees BASELINE page X next to the CURRENT page it was actually matched
    to — not naively paired by page number. This prevents the LLM from
    hallucinating page removals when the current PDF has fewer pages than the
    baseline.

    Content block format (OpenAI / Anthropic compatible):
      {"type": "text", "text": "..."}
      {"type": "image", "mime": "image/jpeg", "b64": "...", "label": "..."}

    The LLM client translates these into provider-specific payloads.
    """
    if mode != "benchmark" or not (baseline_images or current_images):
        return text_content

    blocks: list = [{"type": "text", "text": text_content}]

    baseline_map = {img["page"]: img for img in (baseline_images or [])}
    current_map  = {img["page"]: img for img in (current_images or [])}

    # Build alignment-aware pairs from visual_diffs when available.
    # Each entry maps (baseline_page, current_page, op) so we label images with
    # their alignment context rather than raw page numbers.
    pairs: list = []  # list of (baseline_pg | None, current_pg | None, label_prefix)
    if visual_diffs:
        for v in visual_diffs:
            op = str(v.get("alignment_op") or "matched").lower()
            exp_pg = v.get("expected_page_num")
            act_pg = v.get("actual_page_num")
            pg     = v.get("page")

            if op == "deleted":
                b_pg = exp_pg if exp_pg is not None else pg
                pairs.append((b_pg, None, f"BASELINE page {b_pg} — DELETED (no match in current)"))
            elif op == "inserted":
                c_pg = act_pg if act_pg is not None else pg
                pairs.append((None, c_pg, f"CURRENT page {c_pg} — INSERTED (no match in baseline)"))
            else:
                b_pg = exp_pg if exp_pg is not None else pg
                c_pg = act_pg if act_pg is not None else pg
                label = f"BASELINE page {b_pg} matched to CURRENT page {c_pg}"
                pairs.append((b_pg, c_pg, label))
    else:
        # Fallback: pair by page number
        all_pages = sorted(set(baseline_map) | set(current_map))
        for pg in all_pages:
            pairs.append((pg, pg, f"page {pg}"))

    for b_pg, c_pg, label in pairs:
        if b_pg is not None and b_pg in baseline_map:
            blocks.append({
                "type": "image",
                "mime": baseline_map[b_pg]["mime"],
                "b64":  baseline_map[b_pg]["b64"],
                "label": f"BASELINE — {label}",
            })
        if c_pg is not None and c_pg in current_map:
            blocks.append({
                "type": "image",
                "mime": current_map[c_pg]["mime"],
                "b64":  current_map[c_pg]["b64"],
                "label": f"CURRENT — {label}",
            })

    return blocks


# ---------------------------------------------------------------------------
# Vision-first comparison prompt
# ---------------------------------------------------------------------------

def build_vision_prompt(
    baseline_images: List[Dict[str, Any]],
    current_images: List[Dict[str, Any]],
) -> List[dict]:
    """
    Build a minimal multimodal prompt that sends PDF pages directly to the LLM
    and asks for plain factual observations — no classification, no severity.

    The LLM receives interleaved BASELINE / CURRENT page pairs so it can
    compare corresponding pages side-by-side.

    Returns an OpenAI-style messages list.
    """
    b_map = {img["page"]: img for img in (baseline_images or [])}
    c_map = {img["page"]: img for img in (current_images or [])}

    all_pages = sorted(set(b_map) | set(c_map))

    blocks: List[Dict[str, Any]] = []
    blocks.append({
        "type": "text",
        "text": (
            "You are reviewing two versions of an insurance or financial PDF form.\n"
            "BASELINE = the original / golden-copy version.\n"
            "CURRENT  = the version under review.\n\n"
            "The pages below are interleaved: BASELINE page N is followed by CURRENT page N.\n"
        ),
    })

    for pg in all_pages:
        if pg in b_map:
            blocks.append({
                "type": "image",
                "mime": b_map[pg]["mime"],
                "b64":  b_map[pg]["b64"],
                "label": f"BASELINE page {pg}",
            })
        if pg in c_map:
            blocks.append({
                "type": "image",
                "mime": c_map[pg]["mime"],
                "b64":  c_map[pg]["b64"],
                "label": f"CURRENT page {pg}",
            })

    blocks.append({
        "type": "text",
        "text": (
            "\nCompare the two document versions and list EVERY difference you observe.\n\n"
            "OBSERVATION RULES:\n"
            "- State what changed in plain English. One clear sentence per observation.\n"
            "- Group related field changes on the same page into a single observation "
            "(e.g. 'Page 1 fields filled in: Named Insured = X, Date = Y, Amount = Z').\n"
            "- For watermarks: read the watermark text character-by-character and quote it "
            "exactly as it appears — do not paraphrase or guess the word.\n"
            "- For page-count changes: note the old and new counts but do NOT create a "
            "separate observation for every re-paginated line of text.\n"
            "- Skip observations about content that shifted purely due to page consolidation "
            "if no text actually changed.\n"
            "- Include: logos, colours, watermarks, signatures, structural layout changes.\n"
            "- Do NOT judge whether a change is good or bad.\n\n"
            'current_page must be the page number in the CURRENT document only (e.g. "1", "2", '
            '"1-3", or "all"). Use "all" when the change appears on every page.\n\n'
            "Respond ONLY with valid JSON — no text outside the JSON block:\n"
            "{\n"
            '  "observations": [\n'
            '    {\n'
            '      "current_page": "1",\n'
            '      "observation": "Brief plain-English description of what changed.",\n'
            '      "confidence": "certain | likely | possible"\n'
            '    }\n'
            '  ],\n'
            '  "summary": "One-sentence overall summary of the differences found."\n'
            "}"
        ),
    })

    return [
        {
            "role": "system",
            "content": (
                "You are an expert document analyst comparing insurance PDF forms. "
                "You return ONLY strict JSON with no commentary outside the JSON block."
            ),
        },
        {"role": "user", "content": blocks},
    ]
