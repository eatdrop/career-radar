FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_HOST=0.0.0.0 \
    APP_PORT=3000 \
    APP_DATA_DIR=/data

RUN groupadd --system app && useradd --system --gid app --create-home app

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY web ./web
RUN pip install --no-cache-dir ".[ai,files]"

RUN mkdir -p /data && chown -R app:app /data
USER app

EXPOSE 3000
VOLUME ["/data"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:3000/readyz', timeout=3)"

CMD ["jobsearch-web"]
