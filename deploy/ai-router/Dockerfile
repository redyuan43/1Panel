FROM python:3.12.8-slim

ARG SOURCE_REVISION
ARG SOURCE_TREE_SHA256
RUN test "${#SOURCE_REVISION}" -eq 40 \
    && test "${#SOURCE_TREE_SHA256}" -eq 64 \
    && ! printf '%s%s' "$SOURCE_REVISION" "$SOURCE_TREE_SHA256" \
        | grep -q '[^0-9a-f]'
LABEL org.opencontainers.image.revision=$SOURCE_REVISION \
    org.opencontainers.image.source-tree-sha256=$SOURCE_TREE_SHA256

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY ai_router /app/ai_router
COPY config /app/config
COPY benchmarks /app/benchmarks

RUN useradd --create-home --uid 10001 router \
    && mkdir -p /data/audit \
    && chmod -R a+rX /app/ai_router /app/config /app/benchmarks \
    && chown -R router:router /data

USER router
