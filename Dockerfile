FROM python:3.12-slim

WORKDIR /app

# Install system deps
RUN apt-get update && apt-get install -y --no-install-recommends gcc && \
    rm -rf /var/lib/apt/lists/*

# Copy everything needed for install
COPY pyproject.toml ./
COPY src/ src/
COPY alembic.ini ./
COPY alembic/ alembic/

# Install Python deps + app
RUN pip install --no-cache-dir . && pip install --no-cache-dir gunicorn

# Create non-root user and data directory
RUN useradd -r -s /bin/false appuser && mkdir -p /data && chown appuser:appuser /data
USER appuser

ENV DATABASE_URL=sqlite:////data/yafuama.db
ENV LOG_LEVEL=INFO
ENV PORT=8001
ENV HOST=0.0.0.0

EXPOSE ${PORT}

CMD gunicorn yafuama.main:app \
    -k uvicorn.workers.UvicornWorker \
    --bind ${HOST}:${PORT} \
    --workers 1 \
    --timeout 120
