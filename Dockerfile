FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

CMD ["sh", "-c", "alembic upgrade head && if [ \"$SEED_DEMO\" = \"true\" ]; then python -m app.seed; fi && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
