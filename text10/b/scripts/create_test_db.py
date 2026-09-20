"""确保测试数据库存在存在于目标 PostgreSQL 实例中。"""
import os
import re
from urllib.parse import urlparse

import psycopg

url = os.environ["TEST_DATABASE_URL"]
# postgresql+psycopg:// -> postgresql://
maint_url = re.sub(r"^postgresql\+[^:]+://", "postgresql://", url)
parsed = urlparse(maint_url)
test_db = parsed.path.lstrip("/")

admin = parsed._replace(path="/postgres").geturl()
with psycopg.connect(admin, autocommit=True) as conn:
    exists = conn.execute(
        "SELECT 1 FROM pg_database WHERE datname = %s", (test_db,)
    ).fetchone()
    if not exists:
        conn.execute(f'CREATE DATABASE "{test_db}"')
        print(f"created database {test_db}")
    else:
        print(f"database {test_db} already exists")
