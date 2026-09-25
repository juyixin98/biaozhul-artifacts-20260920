"""支持 ``python -m tlog`` 调用 CLI。"""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
