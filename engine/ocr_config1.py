# engine/ocr_config.py
import os

# Try to import pytesseract - it's optional and only needed for OCR
pytesseract = None
try:
    import pytesseract as _pytesseract
    pytesseract = _pytesseract
except ImportError:
    pytesseract = None


def configure_tesseract():
    """
    Configure pytesseract to use Tesseract-OCR binary.
    Returns the path if successful, None if not available.
    """
    if pytesseract is None:
        print("WARNING: pytesseract not installed. OCR functionality will be unavailable.")
        return None

    tesseract_path = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

    if os.path.exists(tesseract_path):
        pytesseract.pytesseract.tesseract_cmd = tesseract_path
        return tesseract_path

    print(f"WARNING: Tesseract not found at {tesseract_path}. OCR functionality will be unavailable.")
    print("Install Tesseract OCR from: https://github.com/UB-Mannheim/tesseract/wiki")
    return None
