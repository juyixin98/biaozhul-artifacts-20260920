import os

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+psycopg://civicledger:civicledger@localhost:56510/civicledger",
)

# 演示令牌：仅用于本地/开发。生产环境必须替换并放在网关或身份提供者之后。
MANAGER_TOKEN = os.environ.get("MANAGER_TOKEN", "dev-manager-token")
ACCOUNTANT_TOKEN = os.environ.get("ACCOUNTANT_TOKEN", "dev-accountant-token")
AUDITOR_TOKEN = os.environ.get("AUDITOR_TOKEN", "dev-auditor-token")

# 单行/单凭证允许的最大金额（分）。远小于 BIGINT 上限，防止汇总溢出。
MAX_AMOUNT_CENTS = 999_999_999_999_999  # 约 1 万亿元
