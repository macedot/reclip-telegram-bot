FROM python:3.12-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# N2 — install from requirements.txt instead of inline pip install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# L8 — run as a non-root user
RUN groupadd -r app && useradd -r -g app -d /app -s /sbin/nologin app \
    && mkdir -p /downloads /data && chown -R app:app /app /downloads /data

USER app

EXPOSE 8899
ENV HOST=0.0.0.0
ENV DOWNLOADS_PATH=/downloads
CMD ["python", "app.py"]
