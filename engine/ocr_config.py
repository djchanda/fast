import os

pytesseract = None
try:
    import pytesseract as _pytesseract
    pytesseract = _pytesseract
except ImportError:
    pytesseract = None


def configure_tesseract():
    """
    Configure pytesseract to use Tesseract-OCR binary.
    Order:
    1) TESSERACT_CMD env var
    2) Common Windows install path
    Returns configured path or None.
    """
    if pytesseract is None:
        print("WARNING: pytesseract not installed. OCR functionality will be unavailable.")
        return None

    candidates = [
        os.getenv("TESSERACT_CMD", "").strip(),
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    ]

    for path in candidates:
        if path and os.path.exists(path):
            pytesseract.pytesseract.tesseract_cmd = path
            return path

    print("WARNING: Tesseract executable not found. OCR functionality will be unavailable.")
    print("Set TESSERACT_CMD in .env or install Tesseract OCR.")
    return None