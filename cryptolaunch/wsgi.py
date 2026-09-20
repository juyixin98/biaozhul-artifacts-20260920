"""WSGI 入口：模块加载时（即进程启动时）从数据库重建内存订单簿。"""
import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cryptolaunch.settings")

application = get_wsgi_application()

# 应用初始化完成后重建订单簿。必须在 migrate/init_sim_data 之后由 Web 进程触发，
# 因此放在 wsgi 模块而不是 AppConfig.ready（manage.py 命令也会触发 ready）。
from trading.engine import bootstrap_order_book  # noqa: E402

bootstrap_order_book()
