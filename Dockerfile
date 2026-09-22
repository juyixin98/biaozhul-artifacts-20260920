FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt /app/
RUN pip install -r requirements.txt

COPY . /app

RUN chmod +x /app/entrypoint.sh

EXPOSE 8000

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["gunicorn", "fieldsnap.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3", "--timeout", "120"]
