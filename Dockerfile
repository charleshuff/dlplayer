FROM python:3.14-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
# Self-hosted so that a viewer's browser never talks to a third party.
ADD --chmod=644 --checksum=sha256:cfc70cc9e9fc63d071919e087670abc760c8fd1bc889b88588026e4d22193bbe \
    https://cdn.jsdelivr.net/npm/hls.js@1.7.4/dist/hls.min.js ./app/static/hls.min.js

# yt-dlp itself is deliberately not baked in: app/updater.py installs it into
# /data on first start and keeps it current from then on.
RUN useradd --uid 1000 --home-dir /data app && mkdir /data && chown app:app /data
USER app
ENV HOME=/data XDG_CACHE_HOME=/data/cache PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
VOLUME /data
EXPOSE 8080
CMD ["python", "-m", "app.server"]
