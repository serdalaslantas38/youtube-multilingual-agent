# syntax=docker/dockerfile:1
# Single image for the modular monolith. The same image runs the API
# (default CMD) and the background worker (overridden command, Stage 8).

FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv

# Non-root runtime user.
RUN groupadd --system app && useradd --system --gid app --home /srv app

# Install dependencies first for better layer caching.
COPY pyproject.toml README.md ./
COPY backend ./backend
RUN pip install .

COPY config ./config

USER app
ENV CONFIG_DIR=/srv/config \
    PORT=8000

EXPOSE 8000

# app.main:app is created in Stage 2 (FastAPI foundation).
# Cloud Run injects $PORT; default to 8000 locally.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --proxy-headers"]

