"""全局配置：所有参数均可通过环境变量覆盖。"""
import os


def _get_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw is not None else default


# —— HMAC 请求签名 ——
API_KEY_ID = os.environ.get("DISPATCH_API_KEY_ID", "demo-key-1")
API_SECRET = os.environ.get("DISPATCH_API_SECRET", "demo-secret-key")
TIMESTAMP_WINDOW_S = int(os.environ.get("DISPATCH_TIMESTAMP_WINDOW_S", "300"))

# —— 数据库 ——
DB_PATH = os.environ.get(
    "DISPATCH_DB_PATH", os.path.join(os.getcwd(), "dispatch.db")
)

# —— 能耗模型（合成模型，单位见 README）——
BASE_RATE_WH_PER_M = _get_float("DISPATCH_BASE_RATE", 0.18)
LOAD_RATE_WH_PER_M_KG = _get_float("DISPATCH_LOAD_RATE", 0.0006)
WAIT_RATE_WH_PER_S = _get_float("DISPATCH_WAIT_RATE", 0.012)

# —— 安全余量 ——
DEFAULT_SAFETY_MARGIN_WH = _get_float("DISPATCH_SAFETY_MARGIN_WH", 20.0)

# —— 实测电量相对预测的告警容差（比例）——
MEASUREMENT_TOLERANCE = _get_float("DISPATCH_MEASUREMENT_TOLERANCE", 0.01)
