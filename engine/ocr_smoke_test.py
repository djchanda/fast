import pytesseract
import pdfplumber
import io

pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

with open("scanned.pdf", "rb") as f:
    pdf_bytes = f.read()

with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
    page = pdf.pages[0]
    img = page.to_image(resolution=300).original
    img = img.convert("L")

    text = pytesseract.image_to_string(img, lang="eng").strip()
    print(text[:1200])
