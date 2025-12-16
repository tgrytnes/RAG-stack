FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      tesseract-ocr \
      qpdf \
      ghostscript \
      pngquant \
      libxml2 \
      libxslt1.1 \
      libmagic1 \
      poppler-utils \
      ocrmypdf \
      wget \
      file \
      ca-certificates && \
    rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
    ocrmypdf \
    pdfplumber \
    pillow \
    pytesseract \
    mail-parser

WORKDIR /app

COPY app.py .

CMD ["python", "app.py"]
