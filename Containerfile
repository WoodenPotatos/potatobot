# PotatoBot container image.
#
# One image serves both roles. The bot is the default; the dashboard is the same
# image started with a different command, which is what lets the two run as
# separate supervised services against one shared database volume.
#
# Build:  podman build -t potatobot:2.0.0-rc.1 -f Containerfile .
# Bot:    podman run --env-file .env -v potatobot-data:/data potatobot:2.0.0-rc.1
# Panel:  podman run --env-file .env -v potatobot-data:/data -p 127.0.0.1:5000:5000 \
#             potatobot:2.0.0-rc.1 python dashboard_api.py

FROM python:3.13-slim AS base

# ffmpeg is required for music playback; the rest keeps the layer small.
RUN apt-get update \
    && apt-get install --yes --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Never run as root, and give the account a real home so caches land somewhere
# writable rather than in the image.
RUN useradd --create-home --uid 10001 potatobot

WORKDIR /app

# Dependencies first, so a source change does not invalidate the install layer.
COPY requirements.lock ./
RUN python -m pip install --no-cache-dir --requirement requirements.lock

COPY --chown=potatobot:potatobot . .

# The database lives on a volume, never in the image, so an upgrade cannot
# replace it and a rollback cannot lose it.
ENV POTATOBOT_DB_PATH=/data/economy.db \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
RUN mkdir -p /data && chown potatobot:potatobot /data
VOLUME ["/data"]

USER potatobot

# Fails the container if the schema cannot be opened, which is the failure that
# matters most on startup.
HEALTHCHECK --interval=60s --timeout=10s --start-period=20s --retries=3 \
    CMD python -c "import os, sqlite3; sqlite3.connect('file:' + os.environ['POTATOBOT_DB_PATH'] + '?mode=ro', uri=True).execute('PRAGMA user_version')"

CMD ["python", "main.py"]
