# FAST – AI Assisted Forms Testing (Starter)

This is the starter scaffold for the full FAST web utility.

## What you have right now
- Flask web app with CGI/BMO styling (header/footer, watermark, button colors)
- Landing/Login (stub)
- Project Dashboard (create + list + open)
- Project Overview with left navigation
- Contact Us page with SVP/VP/Director cards
- Database models (SQLite) ready for Forms/TestCases/Runs/Results
- Your existing PDF extraction + Gemini validation engine copied under `engine/`

## Quick start (PyCharm-friendly)

1) **Create a virtualenv** (PyCharm can do this automatically):

```bash
python -m venv .venv
```

2) **Activate it**

Windows (PowerShell):
```powershell
.\.venv\Scripts\Activate.ps1
```

Mac/Linux:
```bash
source .venv/bin/activate
```

3) **Install deps**
```bash
pip install -r requirements.txt
```

4) **Create your `.env`**
Copy `.env.example` to `.env` and set `GEMINI_API_KEY`.

5) **Run**
```bash
python run.py
```
Open: http://localhost:5000

## How we will build the product (next milestones)
1) Forms page + upload storage (max 10)
2) Test case designer (Basic/Specific/Benchmark)
3) Execute page (multi-select) wired to Gemini engine
4) Results + history + report downloads
5) Hardening (RBAC, audit log, evidence bundle)
