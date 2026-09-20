FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv/app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

RUN chmod +x docker/start.sh

EXPOSE 8000

CMD ["docker/start.sh"]
