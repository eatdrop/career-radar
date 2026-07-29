FROM python:3.14-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY web ./web
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir ".[ai,files]"

FROM python:3.14-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:$PATH \
    APP_HOST=0.0.0.0 \
    APP_PORT=3000 \
    APP_DATA_DIR=/data

RUN groupadd --system app && useradd --system --gid app --create-home app

COPY --from=builder /opt/venv /opt/venv

RUN mkdir -p /data && chown -R app:app /data
WORKDIR /data
USER app

EXPOSE 3000
VOLUME ["/data"]
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:3000/readyz', timeout=3)"

CMD ["jobsearch-web"]
