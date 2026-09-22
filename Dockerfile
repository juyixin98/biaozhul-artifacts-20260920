FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    KEX_DB_URL=sqlite:////data/kex.db \
    KEX_ROLE=api

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY rules ./rules
COPY migrations ./migrations

RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8080

HEALTHCHECK --interval=10s --timeout=3s --retries=10 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2).status==200 else 1)"

# 启动前迁移 + 内置规则，随后 Waitress 提供服务（内置 2 个工作器线程）
CMD ["sh", "-c", "python -m app.wsgi"]
