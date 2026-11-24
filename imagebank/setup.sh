#!/usr/bin/env bash
# Installs what the image bank pipeline needs (idempotent).
set -e
if ! command -v tesseract >/dev/null || ! tesseract --list-langs 2>/dev/null | grep -q ara; then
  apt-get update -q >/dev/null 2>&1 || true
  apt-get install -y -q tesseract-ocr tesseract-ocr-ara >/dev/null
fi
pip install -q "opencv-python-headless<5" pytesseract imagehash requests pillow numpy
