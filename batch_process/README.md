# FAST Batch Process

AI-powered PDF form validation — no browser, no database, no web server.

Drop PDFs in two folders, edit a manifest file, run one command, get HTML reports.

---

## Folder Structure

```
batch_process/
├── run_batch.py          ← ENTRY POINT  (right-click → Run in PyCharm)
├── manifest.yaml         ← EDIT THIS to define your tests
├── .env                  ← ADD YOUR API KEY here (copy from .env.example)
├── forms/
│   ├── current/          ← Drop your updated / new PDFs here
│   └── benchmark/        ← Drop your baseline / golden copy PDFs here
├── reports/              ← HTML reports are written here after each run
└── batch/                ← Internal modules (do not edit)
```

---

## Quick Start (5 steps)

### Step 1 — Install prerequisites

```bash
# From the repo root (not batch_process/)
pip install -r requirements.txt

# System tools (install once — see details below)
# macOS:    brew install tesseract poppler
# Ubuntu:   sudo apt install tesseract-ocr poppler-utils
# Windows:  see prerequisites section below
```

### Step 2 — Set up your API key

```bash
cd batch_process/
cp .env.example .env
# Open .env and add your GEMINI_API_KEY (or OPENAI_API_KEY / ANTHROPIC_API_KEY)
```

### Step 3 — Drop your PDFs

```
forms/current/    ← current version of the form  (e.g. policy_v2.pdf)
forms/benchmark/  ← baseline / golden copy       (e.g. policy_v1.pdf)
```

### Step 4 — Edit manifest.yaml

```yaml
project:
  name: "My Project"

settings:
  mode: benchmark
  llm_provider: gemini

tests:
  - name: "Policy Form Check"
    current:   current/policy_v2.pdf
    benchmark: benchmark/policy_v1.pdf
    mode: benchmark
```

### Step 5 — Run

```bash
python run_batch.py
```

Or in **PyCharm**: right-click `run_batch.py` → **Run 'run_batch'**

---

## What You See in the Terminal

```
────────────────────────────────────────────────────────────
  FAST — AI Assisted Forms Testing  |  Batch Mode
────────────────────────────────────────────────────────────

  Project    : Liberty Mutual Q1 Forms Review
  Environment: QA
  Tests      : 3
  Provider   : gemini

  [1/3] Running: Policy Form A — Version Comparison ...
  ✓ PASS     Policy Form A — Version Comparison        0E 2W
             Report → /path/to/reports/Policy_Form_A_20260401_142301.html
             4.2s

  [2/3] Running: Application Form B — Quality Check ...
  ✗ FAIL     Application Form B — Quality Check        5E 1W
             Report → /path/to/reports/Application_Form_B_20260401_142345.html
             3.8s

  ────────────────────────────────────────────────────────────
  FAST Batch — Summary
  ────────────────────────────────────────────────────────────
  Total tests : 3
  PASS   : 2
  FAIL   : 1
```

---

## Manifest Reference

```yaml
project:
  name: "My Project"         # Shown in HTML reports
  environment: "QA"          # Optional — shown in reports
  account: "Liberty Mutual"  # Optional — shown in reports

settings:
  mode: benchmark            # Default mode: basic | specific | benchmark
  llm_provider: gemini       # gemini | openai | claude
  output_dir: ./reports      # Where to write HTML reports
  fail_on_critical: true     # Exit code 1 if CRITICAL/FAIL found

tests:
  - name: "Test display name"
    current:   current/my_form.pdf          # Required — path under forms/
    benchmark: benchmark/my_baseline.pdf    # Required for benchmark mode
    mode: benchmark                         # Overrides the global default
    prompt: "Optional custom rules..."      # Used in specific mode
```

### Modes Explained

| Mode | What it does | Needs benchmark? |
|---|---|---|
| `benchmark` | Compares current form against a baseline; detects any change | Yes |
| `basic` | Standalone AI quality inspection — no baseline required | No |
| `specific` | Like basic, but driven by your `prompt` validation rules | No |

### PDF Path Rules

Paths in the manifest are relative to the `forms/` folder:
- `current/policy.pdf` → `batch_process/forms/current/policy.pdf`
- `benchmark/policy.pdf` → `batch_process/forms/benchmark/policy.pdf`
- Absolute paths also work: `/home/user/documents/policy.pdf`

---

## CLI Options

```bash
python run_batch.py [OPTIONS]

Options:
  --manifest PATH   Path to manifest file (default: manifest.yaml)
  --output   DIR    Override output directory for HTML reports
  --help            Show this message and exit

Examples:
  python run_batch.py
  python run_batch.py --manifest q1_tests.yaml
  python run_batch.py --manifest manifest.yaml --output /tmp/reports
```

---

## HTML Reports

Each test produces one HTML report in `reports/`. Open it in any browser.

The report includes:
- **Verdict**: PASS / REVIEW / FAIL at the top
- **Execution Summary**: overall assessment and metrics
- **Report Info**: both PDF filenames, timestamp
- **Page-by-Page Decision Table**: every page with status, severity, decision, evidence, and visual similarity score
- **Filter buttons**: show All / Mismatch / Review pages
- **Snapshot thumbnails**: visual diff images where available (benchmark mode)

---

## Prerequisites — Detailed Setup

### Python

**Required: Python 3.10 or newer**

Check your version:
```bash
python --version
```

### Python Packages

All packages are in the repo's `requirements.txt`. Install once from the repo root:
```bash
pip install -r requirements.txt
```

Key packages used by the batch runner:

| Package | Version | Purpose |
|---|---|---|
| `pdfplumber` | ≥0.11 | PDF text and field extraction |
| `pypdf` | ≥5.0 | PDF metadata and page reading |
| `pdf2image` | ≥1.16 | PDF → image for visual comparison (benchmark mode) |
| `Pillow` | ≥10.0 | Image processing |
| `pytesseract` | ≥0.3.10 | OCR for scanned PDFs (fallback) |
| `google-generativeai` | ≥0.7 | Gemini LLM client |
| `openai` | ≥1.30 | OpenAI GPT-4o client |
| `anthropic` | ≥0.25 | Anthropic Claude client |
| `python-dotenv` | ≥1.0 | Load `.env` file |
| `pyyaml` | ≥6.0 | Parse `manifest.yaml` |

### Tesseract OCR

Required for scanned (image-based) PDFs. Not needed if your PDFs have embedded text.

- **macOS**: `brew install tesseract`
- **Ubuntu/Debian**: `sudo apt install tesseract-ocr`
- **Windows**: Download installer from https://github.com/UB-Mannheim/tesseract/wiki
  then add to PATH: `C:\Program Files\Tesseract-OCR`

### Poppler

Required for benchmark mode (visual diff). Converts PDF pages to images.

- **macOS**: `brew install poppler`
- **Ubuntu/Debian**: `sudo apt install poppler-utils`
- **Windows**: Download from https://github.com/oschwartz10612/poppler-windows/releases
  then add the `bin/` folder to your PATH

### LLM API Key

You need one of the following. Gemini has a free tier and is the default.

| Provider | Where to get key | Manifest setting |
|---|---|---|
| Google Gemini | https://aistudio.google.com/app/apikey | `llm_provider: gemini` |
| OpenAI | https://platform.openai.com/api-keys | `llm_provider: openai` |
| Anthropic Claude | https://console.anthropic.com/settings/keys | `llm_provider: claude` |
| AWS Bedrock | Configure `~/.aws/credentials` | (contact your AWS admin) |

Add the key to your `.env` file:
```
GEMINI_API_KEY=AIza...
```

---

## PyCharm Setup

1. Open the **repo root** as the project (not just `batch_process/`)
2. Set interpreter: **File → Settings → Python Interpreter** → select the repo's virtual environment
3. Navigate to `batch_process/run_batch.py`
4. Right-click → **Run 'run_batch'**
5. To configure arguments: **Run → Edit Configurations** → add `--manifest my_tests.yaml` to Parameters

**Tip**: Set the working directory in the Run Configuration to `batch_process/` so relative paths in manifest.yaml resolve correctly.

---

## What FAST Does NOT Do in Batch Mode

The batch runner is intentionally lightweight. The following features are only available in the FAST UI:

| Feature | UI | Batch CLI |
|---|:---:|:---:|
| AI form analysis | ✓ | ✓ |
| HTML reports | ✓ | ✓ |
| Visual diff (benchmark mode) | ✓ | ✓ |
| Finding review workflow | ✓ | — |
| Approval Gate | ✓ | — |
| Jira defect logging | ✓ | — |
| Webhooks / notifications | ✓ | — |
| Scheduled runs | ✓ | — |
| Trend analytics | ✓ | — |
| Role-based access control | ✓ | — |

---

## Troubleshooting

**`ModuleNotFoundError: No module named 'engine'`**
The runner automatically adds the repo root to `sys.path`. Make sure you run from the `batch_process/` directory or use the `--manifest` option with a full path.

**`Missing GEMINI_API_KEY in environment`**
Copy `.env.example` to `.env` and add your key. Make sure you saved the file.

**`PDF not found: forms/current/my_form.pdf`**
Check the filename in `manifest.yaml` matches exactly (case-sensitive on Linux/Mac).

**Visual diff errors in benchmark mode**
Ensure Poppler is installed (`pdf2image` depends on it). Visual diff errors are non-fatal — the LLM analysis still runs and produces a report.

**OCR errors on scanned PDFs**
Install Tesseract and ensure it is on your PATH. Run `tesseract --version` to verify.
