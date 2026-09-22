#!/usr/bin/env python
"""Django 命令行入口。"""
import os
import sys


def main():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "fieldsnap.settings")
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "无法导入 Django，请确认已安装依赖并激活虚拟环境。"
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
