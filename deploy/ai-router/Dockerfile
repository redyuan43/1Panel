FROM python:3.12.8-slim

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
