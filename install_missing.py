#!/usr/bin/env python
"""
Install missing packages for FAST application
"""
import subprocess
import sys

packages_to_install = [
    "pytesseract>=0.3.10",
    "Pillow>=9.1.0",
]

print("Installing missing packages...")
for package in packages_to_install:
    print(f"\nInstalling {package}...")
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", package,
         "--index-url", "https://mirrors.aliyun.com/pypi/simple/",
         "--trusted-host", "mirrors.aliyun.com"],
        capture_output=True,
        text=True
    )

    if result.returncode == 0:
        print(f"✓ {package} installed successfully")
    else:
        print(f"✗ Failed to install {package}")
        print(result.stderr)

print("\nInstallation complete!")
print("\nVerifying imports...")
try:
    import pytesseract
    print("✓ pytesseract imported successfully")
except ImportError as e:
    print(f"✗ pytesseract import failed: {e}")

try:
    import PIL
    print("✓ PIL (Pillow) imported successfully")
except ImportError as e:
    print(f"✗ PIL import failed: {e}")

print("\nNote: To use OCR functionality, you also need to install Tesseract-OCR binary")
print("Download from: https://github.com/UB-Mannheim/tesseract/wiki")

