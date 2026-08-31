FROM python:3.12-slim@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de

ARG FFMPEG_VERSION=7:7.1.5-0+deb13u1

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    EMBY_THEME_CONFIG=/config/config.yaml

RUN apt-get update \
    && apt-get install -y --no-install-recommends "ffmpeg=${FFMPEG_VERSION}" ca-certificates tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md requirements.lock /app/
RUN PIP_DEFAULT_TIMEOUT=120 PIP_RETRIES=8 python -m pip install --no-cache-dir -r requirements.lock
COPY emby_theme_worker /app/emby_theme_worker
RUN PIP_DEFAULT_TIMEOUT=120 PIP_RETRIES=8 python -m pip install --no-cache-dir --no-deps . \
    && python -c "import yt_dlp; assert yt_dlp.version.__version__ == '2026.08.19'" \
    && ffmpeg -version | grep -F 'ffmpeg version 7.1.5'

RUN mkdir -p /data /config /run/secrets \
    && chown -R 1000:1001 /data /config /run/secrets

USER 1000:1001
ENTRYPOINT ["/usr/bin/tini", "--", "emby-theme-worker"]
CMD ["serve"]
