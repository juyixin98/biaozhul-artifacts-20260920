FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TASK_LEASE_SECONDS=60

WORKDIR /app

# 系统依赖：spaCy 运行仅需少量库；保留 gcc 头文件最小集合以防 wheel 缺失
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
# 在线构建：安装 Python 依赖与 spaCy 英文模型（NLTK 数据从离线目录拷贝）
RUN pip install --upgrade pip && \
    pip install -r requirements.txt && \
    pip install https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.7.1/en_core_web_sm-3.7.1-py3-none-any.whl

COPY . /app

# 容器内自带离线 NLTK 数据（构建前请先运行 scripts/vendor_offline.sh 生成 offline/）
# 若 offline/nltk_data 已随仓库拷入则直接生效；否则启动时尝试一次下载。
RUN python - <<'PY'
import os, nltk
if os.path.isdir('/app/offline/nltk_data'):
    nltk.data.path.insert(0, '/app/offline/nltk_data')
else:
    for p in ('stopwords', 'punkt_tab', 'punkt'):
        nltk.download(p, download_dir='/app/offline/nltk_data', quiet=True)
PY

EXPOSE 8000

CMD ["sh", "-c", "python manage.py migrate --noinput && gunicorn config.wsgi:application --bind 0.0.0.0:8000 --workers 3"]
