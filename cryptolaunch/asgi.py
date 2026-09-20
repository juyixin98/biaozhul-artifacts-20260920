"""ASGI 入口（本服务为同步实现，保留标准入口供未来扩展）。"""
import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cryptolaunch.settings")

application = get_asgi_application()
