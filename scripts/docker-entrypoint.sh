#!/bin/sh
set -e
alembic upgrade head
python -m scripts.seed_demo
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
