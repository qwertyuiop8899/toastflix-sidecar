FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN useradd --create-home --uid 10001 sidecar \
    && mkdir -p /app/data \
    && chown -R sidecar:sidecar /app
USER sidecar

EXPOSE 3107

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "3107"]
