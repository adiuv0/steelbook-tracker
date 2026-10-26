FROM python:3.12-slim

# Unraid reads these for the Docker tab icon and the "WebUI" menu entry, so a
# container added without the template still gets both.
LABEL net.unraid.docker.webui="http://[IP]:[PORT:8787]/" \
      net.unraid.docker.icon="https://raw.githubusercontent.com/adiuv0/steelbook-tracker/main/app/static/icon.png" \
      org.opencontainers.image.source="https://github.com/adiuv0/steelbook-tracker"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    TZ=America/Los_Angeles

RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app

VOLUME /data
EXPOSE 8787
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/healthz')"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8787"]
