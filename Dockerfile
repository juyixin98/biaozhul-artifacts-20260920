FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# System deps: psycopg2-binary needs no compiler, but keep image minimal.
COPY requirements.txt .
# Pull torch from the PyTorch CPU wheel index so the multi-GB CUDA wheels are
# never downloaded — this service is CPU-only by design. torch already being
# satisfied means the second step won't reinstall it from PyPI.
RUN pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu \
        "torch>=2.2" && \
    pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY scripts ./scripts

# Demo data is generated at container start (see entrypoint) so the mounted
# data directory is populated even on first boot.
RUN chmod +x scripts/entrypoint.sh

EXPOSE 8000

ENTRYPOINT ["scripts/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
