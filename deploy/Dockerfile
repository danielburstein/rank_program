FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng libgl1 libglib2.0-0 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY rankrecovery ./rankrecovery
COPY scripts ./scripts
RUN pip install --no-cache-dir -e ".[server,vision,ocr,bot,avif,postgres]"

ENV RANKRECOVERY_DATA_DIR=/data
VOLUME ["/data"]
EXPOSE 8420
CMD ["rankrecovery-server", "--host", "0.0.0.0", "--port", "8420"]
