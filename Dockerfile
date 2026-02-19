FROM python:3.12-slim

WORKDIR /app

# Install system deps
RUN apt-get update && apt-get install -y --no-install-recommends gcc && \
    rm -rf /var/lib/apt/lists/*

# Install Python deps
COPY pyproject.toml ./
RUN pip install --no-cache-dir . && pip install --no-cache-dir gunicorn

# Copy source
COPY alembic.ini ./
COPY alembic/ alembic/
COPY src/ src/

# Create data directory for SQLite
RUN mkdir -p /data

ENV DATABASE_URL=sqlite:////data/resell_trap.db
ENV LOG_LEVEL=INFO

EXPOSE 8000

CMD ["gunicorn", "resell_trap.main:app", \
     "-k", "uvicorn.workers.UvicornWorker", \
     "--bind", "0.0.0.0:8000", \
     "--workers", "1", \
     "--timeout", "120"]
