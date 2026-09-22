FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    NLTK_DATA=/app/vendor/nltk_data

# mysqlclient needs the MySQL client headers at build time.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        pkg-config \
        default-libmysqlclient-dev \
        curl \
        unzip \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependency layer. With OFFLINE=1 the image is built purely from the
# prepared vendor/ bundle (see scripts/download_offline_deps.sh).
ARG OFFLINE=0
COPY requirements.txt ./
COPY vendor ./vendor
RUN if [ "$OFFLINE" = "1" ]; then \
        pip install --no-index --find-links=/app/vendor/wheels -r requirements.txt \
        && pip install --no-index --find-links=/app/vendor/models en_core_web_sm ; \
    else \
        pip install -r requirements.txt \
        && python -m spacy download en_core_web_sm \
        && python -c "import nltk; nltk.download('punkt', download_dir='/app/vendor/nltk_data'); nltk.download('punkt_tab', download_dir='/app/vendor/nltk_data'); nltk.download('stopwords', download_dir='/app/vendor/nltk_data')" ; \
    fi

COPY . .

EXPOSE 8000

CMD ["gunicorn", "textengine.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3"]
