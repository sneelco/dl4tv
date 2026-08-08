FROM python:3.12-slim

# ffmpeg is required for merging separate video/audio streams, embedding
# metadata/thumbnails and SponsorBlock chapter removal.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates tini \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DL4TV_CONFIG_DIR=/config \
    DL4TV_DOWNLOAD_DIR=/downloads \
    DL4TV_PORT=8484

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY README.md ./

RUN mkdir -p /config /downloads

VOLUME ["/config", "/downloads"]
EXPOSE 8484

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os,sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('DL4TV_PORT','8484')+'/healthz', timeout=4).status==200 else 1)"

ENTRYPOINT ["tini", "--"]
CMD ["python", "-m", "app.main"]
