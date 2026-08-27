FROM python:3.12-slim

# tesseract-ukr/rus/eng — для нарезки сканов; poppler не нужен, PyMuPDF сам рендерит PDF
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr tesseract-ocr-ukr tesseract-ocr-rus \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv

COPY webapp/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY cut_apartments.py ./cut_apartments.py
COPY webapp/app ./app
COPY webapp/backup.py ./backup.py

ENV DATA_DIR=/data \
    PYTHONUNBUFFERED=1 \
    PORT=8000

EXPOSE 8000

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
