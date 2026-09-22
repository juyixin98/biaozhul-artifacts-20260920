import os

import django


def pytest_configure(config):
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "textengine.settings")
    os.environ.setdefault("USE_SQLITE", "1")
    django.setup()
