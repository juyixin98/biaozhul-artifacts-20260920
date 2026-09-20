#!/usr/bin/env python
"""Django 命令行入口。"""
import os
import sys


def main():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cryptolaunch.settings")
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError("未安装 Django，请先安装 requirements.txt") from exc
    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
